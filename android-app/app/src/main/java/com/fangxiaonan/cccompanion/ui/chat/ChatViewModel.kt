package com.fangxiaonan.cccompanion.ui.chat

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.fangxiaonan.cccompanion.CcCompanionApp
import com.fangxiaonan.cccompanion.data.ApiClient
import com.fangxiaonan.cccompanion.data.ChatMessage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class ChatViewModel(application: Application) : AndroidViewModel(application) {

    private val prefs = (application as CcCompanionApp).preferences

    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages: StateFlow<List<ChatMessage>> = _messages.asStateFlow()

    private val _isTyping = MutableStateFlow(false)
    val isTyping: StateFlow<Boolean> = _isTyping.asStateFlow()

    private val _isConnected = MutableStateFlow(false)
    val isConnected: StateFlow<Boolean> = _isConnected.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val _isSending = MutableStateFlow(false)
    val isSending: StateFlow<Boolean> = _isSending.asStateFlow()

    private var pollJob: Job? = null
    private var initialFetchDone = false

    fun startPolling() {
        pollJob?.cancel()
        pollJob = viewModelScope.launch(Dispatchers.IO) {
            while (isActive) {
                poll()
                delay(2000)
            }
        }
    }

    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    private suspend fun poll() {
        val client = ApiClient.create(prefs)
        if (client == null) {
            _isConnected.value = false
            return
        }

        // Check health on first poll
        if (!initialFetchDone) {
            client.healthCheck().onSuccess {
                _isConnected.value = it
            }.onFailure {
                _isConnected.value = false
            }
        }

        // Fetch history using ISO timestamp for proper server-side filtering
        val sinceIso = if (!initialFetchDone) "" else {
            _messages.value.mapNotNull { it.tsIso.takeIf { s -> s.isNotBlank() } }.maxOrNull() ?: ""
        }

        client.getHistory(sinceIso).onSuccess { newMessages ->
            _isConnected.value = true
            _error.value = null

            if (!initialFetchDone) {
                _messages.value = newMessages.sortedBy { it.timestamp }
                initialFetchDone = true
                // Save lastSeenIso for background worker
                val latestIso = newMessages.mapNotNull { it.tsIso.takeIf { s -> s.isNotBlank() } }.maxOrNull() ?: ""
                if (latestIso.isNotBlank()) prefs.lastSeenIso = latestIso
                val latestTs = newMessages.maxOfOrNull { it.timestamp } ?: 0L
                if (latestTs > 0) prefs.lastSeenTimestamp = latestTs
            } else if (newMessages.isNotEmpty()) {
                val currentTs = _messages.value.mapNotNull { it.tsIso.takeIf { s -> s.isNotBlank() } }.toSet()
                val uniqueNew = newMessages.filter { it.tsIso !in currentTs }
                if (uniqueNew.isNotEmpty()) {
                    // Remove only the FIRST matching local optimistic message per server echo
                    val serverUserTexts = uniqueNew.filter { it.role == "user" }.map { it.text }.toMutableList()
                    val cleaned = _messages.value.filter { msg ->
                        if (msg.id.startsWith("local_") && msg.text in serverUserTexts) {
                            serverUserTexts.remove(msg.text)
                            false
                        } else true
                    }
                    _messages.value = (cleaned + uniqueNew).sortedBy { it.timestamp }
                    // Update last seen for background worker
                    val latestTs = _messages.value.maxOfOrNull { it.timestamp } ?: 0L
                    prefs.lastSeenTimestamp = latestTs
                    val latestIso = _messages.value.mapNotNull { it.tsIso.takeIf { s -> s.isNotBlank() } }.maxOrNull() ?: ""
                    if (latestIso.isNotBlank()) prefs.lastSeenIso = latestIso
                }
            }
        }.onFailure {
            _isConnected.value = false
            _error.value = it.message
        }

        // Check typing status
        client.checkTyping().onSuccess { typing ->
            _isTyping.value = typing
        }
    }

    fun sendMessage(text: String) {
        if (text.isBlank()) return

        _isSending.value = true
        viewModelScope.launch(Dispatchers.IO) {

            // Handle slash commands
            val handled = handleSlashCommand(text)
            if (handled) {
                _isSending.value = false
                return@launch
            }

            val client = ApiClient.create(prefs)
            if (client == null) {
                _error.value = "Not configured. Go to Settings."
                _isSending.value = false
                return@launch
            }

            // Show user message locally immediately
            val userMsg = ChatMessage(
                id = "local_${System.currentTimeMillis()}_${text.hashCode()}",
                text = text,
                role = "user",
                timestamp = System.currentTimeMillis()
            )
            _messages.value = _messages.value + userMsg

            try {
                client.sendMessage(text).onSuccess {
                    _error.value = null
                }.onFailure {
                    _error.value = "Send failed: ${it.message}"
                }
            } catch (e: Exception) {
                _error.value = "Error: ${e.message}"
            }

            _isSending.value = false
        }
    }

    private suspend fun handleSlashCommand(text: String): Boolean {
        val client = ApiClient.create(prefs) ?: return false

        return when {
            text.trim() == "/stop" -> {
                client.abort()
                addSystemMessage("Abort signal sent")
                true
            }
            text.trim() == "/restart" -> {
                client.restart()
                addSystemMessage("Restart signal sent")
                true
            }
            text.trim() == "/new" -> {
                client.newSession()
                _messages.value = emptyList()
                initialFetchDone = false
                addSystemMessage("New session started")
                true
            }
            text.trim() == "/compact" -> {
                client.sendMessage("/compact")
                addSystemMessage("Compact command sent")
                true
            }
            else -> false
        }
    }

    private fun addSystemMessage(text: String) {
        val msg = ChatMessage(
            id = "system_${System.currentTimeMillis()}",
            text = "[$text]",
            role = "system",
            timestamp = System.currentTimeMillis()
        )
        _messages.value = _messages.value + msg
    }

    fun clearError() {
        _error.value = null
    }

    fun refresh() {
        initialFetchDone = false
        _messages.value = emptyList()
    }
}
