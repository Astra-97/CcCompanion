package com.fangxiaonan.cccompanion.ui.terminal

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.fangxiaonan.cccompanion.CcCompanionApp
import com.fangxiaonan.cccompanion.data.ApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class TerminalViewModel(application: Application) : AndroidViewModel(application) {

    private val prefs = (application as CcCompanionApp).preferences

    private val _terminalOutput = MutableStateFlow("")
    val terminalOutput: StateFlow<String> = _terminalOutput.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private var pollJob: Job? = null

    fun startPolling() {
        pollJob?.cancel()
        pollJob = viewModelScope.launch(Dispatchers.IO) {
            while (isActive) {
                fetchTerminal()
                delay(3000)
            }
        }
    }

    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    fun manualRefresh() {
        viewModelScope.launch(Dispatchers.IO) {
            _isLoading.value = true
            fetchTerminal()
            _isLoading.value = false
        }
    }

    private suspend fun fetchTerminal() {
        val client = ApiClient.create(prefs)
        if (client == null) {
            _error.value = "Not configured"
            return
        }

        client.getTerminalCapture().onSuccess { output ->
            _terminalOutput.value = output
            _error.value = null
        }.onFailure {
            _error.value = it.message
        }
    }
}
