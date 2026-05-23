package com.fangxiaonan.cccompanion.data

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

class ApiClient(
    private val baseUrl: String,
    private val authToken: String
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .build()

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    fun sendMessage(text: String): Result<String> {
        return try {
            val json = JSONObject().put("text", text).toString()
            val body = json.toRequestBody(jsonMediaType)
            val request = Request.Builder()
                .url("$baseUrl/chat/send")
                .addHeader("X-Auth-Token", authToken)
                .post(body)
                .build()

            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                Result.success(response.body?.string() ?: "")
            } else {
                Result.failure(Exception("HTTP ${response.code}: ${response.message}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun getHistory(sinceIso: String): Result<List<ChatMessage>> {
        return try {
            val urlBuilder = baseUrl.toHttpUrl().newBuilder()
                .addPathSegments("chat/history")
            if (sinceIso.isNotBlank()) urlBuilder.addQueryParameter("since", sinceIso)
            urlBuilder.addQueryParameter("limit", "200")
            val request = Request.Builder()
                .url(urlBuilder.build())
                .addHeader("X-Auth-Token", authToken)
                .get()
                .build()

            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val bodyStr = response.body?.string() ?: "[]"
                val messages = parseMessages(bodyStr)
                Result.success(messages)
            } else {
                Result.failure(Exception("HTTP ${response.code}: ${response.message}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun checkTyping(): Result<Boolean> {
        return try {
            val request = Request.Builder()
                .url("$baseUrl/chat/typing")
                .addHeader("X-Auth-Token", authToken)
                .get()
                .build()

            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val bodyStr = response.body?.string() ?: "{}"
                val json = JSONObject(bodyStr)
                Result.success(json.optBoolean("is_typing", false))
            } else {
                Result.failure(Exception("HTTP ${response.code}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun getTerminalCapture(session: String = "cctg", lines: Int = 120): Result<String> {
        return try {
            val request = Request.Builder()
                .url("$baseUrl/tmux/capture?session=$session&lines=$lines")
                .addHeader("X-Auth-Token", authToken)
                .get()
                .build()

            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                val bodyStr = response.body?.string() ?: ""
                // Try to parse as JSON, fall back to raw text
                try {
                    val json = JSONObject(bodyStr)
                    Result.success(json.optString("content", bodyStr))
                } catch (e: Exception) {
                    Result.success(bodyStr)
                }
            } else {
                Result.failure(Exception("HTTP ${response.code}: ${response.message}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun abort(): Result<String> {
        return postCommand("/chain/abort")
    }

    fun restart(): Result<String> {
        return postCommand("/chain/restart")
    }

    fun newSession(): Result<String> {
        return postCommand("/chain/new_session")
    }

    fun healthCheck(): Result<Boolean> {
        return try {
            val request = Request.Builder()
                .url("$baseUrl/health")
                .get()
                .build()

            val response = client.newCall(request).execute()
            Result.success(response.isSuccessful)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    private fun postCommand(path: String): Result<String> {
        return try {
            val body = "{}".toRequestBody(jsonMediaType)
            val request = Request.Builder()
                .url("$baseUrl$path")
                .addHeader("X-Auth-Token", authToken)
                .post(body)
                .build()

            val response = client.newCall(request).execute()
            if (response.isSuccessful) {
                Result.success(response.body?.string() ?: "")
            } else {
                Result.failure(Exception("HTTP ${response.code}: ${response.message}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    private fun parseMessages(jsonStr: String): List<ChatMessage> {
        val messages = mutableListOf<ChatMessage>()
        try {
            val root = JSONObject(jsonStr)
            val array = root.getJSONArray("records")
            for (i in 0 until array.length()) {
                val obj = array.getJSONObject(i)
                val tsStr = obj.optString("ts", "")
                val ts = parseIsoTimestamp(tsStr)
                val role = obj.optString("role", obj.optString("sender", "assistant"))
                val text = obj.optString("text", obj.optString("content", ""))
                val thinking = obj.optString("thinking", "")
                val tools = obj.optString("tools", "")
                messages.add(
                    ChatMessage(
                        id = obj.optString("id", "${role}_${ts}_${text.hashCode()}"),
                        text = text,
                        role = role,
                        timestamp = ts,
                        tsIso = tsStr,
                        thinking = thinking,
                        tools = tools
                    )
                )
            }
        } catch (e: Exception) {
            // If parsing fails, return empty list
        }
        return messages
    }

    private fun parseIsoTimestamp(iso: String): Long {
        if (iso.isBlank()) return System.currentTimeMillis()
        return try {
            val fmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
            fmt.timeZone = TimeZone.getTimeZone("UTC")
            val cleaned = iso.replace(Regex("\\.[0-9]+.*"), "")
            fmt.parse(cleaned)?.time ?: System.currentTimeMillis()
        } catch (e: Exception) {
            System.currentTimeMillis()
        }
    }

    companion object {
        fun create(prefs: AppPreferences): ApiClient? {
            if (!prefs.isConfigured) return null
            return ApiClient(
                baseUrl = prefs.serverUrl.trimEnd('/'),
                authToken = prefs.authToken
            )
        }
    }
}
