package com.fangxiaonan.cccompanion.data

import android.content.Context
import android.content.SharedPreferences

class AppPreferences(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("cc_companion_prefs", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString(KEY_SERVER_URL, "") ?: ""
        set(value) = prefs.edit().putString(KEY_SERVER_URL, value).apply()

    var authToken: String
        get() = prefs.getString(KEY_AUTH_TOKEN, "") ?: ""
        set(value) = prefs.edit().putString(KEY_AUTH_TOKEN, value).apply()

    var lastSeenTimestamp: Long
        get() = prefs.getLong(KEY_LAST_SEEN_TS, 0L)
        set(value) = prefs.edit().putLong(KEY_LAST_SEEN_TS, value).apply()

    var lastSeenIso: String
        get() = prefs.getString(KEY_LAST_SEEN_ISO, "") ?: ""
        set(value) = prefs.edit().putString(KEY_LAST_SEEN_ISO, value).apply()

    var bubbleFontSize: Int
        get() = prefs.getInt(KEY_BUBBLE_FONT_SIZE, 14)
        set(value) = prefs.edit().putInt(KEY_BUBBLE_FONT_SIZE, value.coerceIn(10, 22)).apply()

    var bubbleColor: String
        get() = prefs.getString(KEY_BUBBLE_COLOR, "5B4A8A") ?: "5B4A8A"
        set(value) = prefs.edit().putString(KEY_BUBBLE_COLOR, value).apply()

    var assistantBubbleColor: String
        get() = prefs.getString(KEY_ASSISTANT_BUBBLE_COLOR, "262638") ?: "262638"
        set(value) = prefs.edit().putString(KEY_ASSISTANT_BUBBLE_COLOR, value).apply()

    var userTextColor: String
        get() = prefs.getString(KEY_USER_TEXT_COLOR, "F0E8FF") ?: "F0E8FF"
        set(value) = prefs.edit().putString(KEY_USER_TEXT_COLOR, value).apply()

    var assistantTextColor: String
        get() = prefs.getString(KEY_ASSISTANT_TEXT_COLOR, "D8D8E8") ?: "D8D8E8"
        set(value) = prefs.edit().putString(KEY_ASSISTANT_TEXT_COLOR, value).apply()

    val isConfigured: Boolean
        get() = serverUrl.isNotBlank() && authToken.isNotBlank()

    companion object {
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_AUTH_TOKEN = "auth_token"
        private const val KEY_LAST_SEEN_TS = "last_seen_timestamp"
        private const val KEY_LAST_SEEN_ISO = "last_seen_iso"
        private const val KEY_BUBBLE_FONT_SIZE = "bubble_font_size"
        private const val KEY_BUBBLE_COLOR = "bubble_color"
        private const val KEY_ASSISTANT_BUBBLE_COLOR = "assistant_bubble_color"
        private const val KEY_USER_TEXT_COLOR = "user_text_color"
        private const val KEY_ASSISTANT_TEXT_COLOR = "assistant_text_color"
    }
}
