package com.fangxiaonan.cccompanion.data

data class ChatMessage(
    val id: String,
    val text: String,
    val role: String, // "user" or "assistant"
    val timestamp: Long,
    val tsIso: String = "",
    val isTyping: Boolean = false,
    val thinking: String = "",
    val tools: String = ""
)
