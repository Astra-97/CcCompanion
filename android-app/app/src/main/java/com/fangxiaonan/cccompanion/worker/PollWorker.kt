package com.fangxiaonan.cccompanion.worker

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.fangxiaonan.cccompanion.CcCompanionApp
import com.fangxiaonan.cccompanion.MainActivity
import com.fangxiaonan.cccompanion.R
import com.fangxiaonan.cccompanion.data.ApiClient
import com.fangxiaonan.cccompanion.data.AppPreferences

class PollWorker(
    context: Context,
    workerParams: WorkerParameters
) : CoroutineWorker(context, workerParams) {

    override suspend fun doWork(): Result {
        val prefs = AppPreferences(applicationContext)
        val client = ApiClient.create(prefs) ?: return Result.success()

        val lastSeenTs = prefs.lastSeenTimestamp
        val lastSeenIso = prefs.lastSeenIso
        val result = client.getHistory(lastSeenIso)

        result.onSuccess { messages ->
            val newAssistantMessages = messages.filter {
                it.role == "assistant" && it.timestamp > lastSeenTs
            }

            if (newAssistantMessages.isNotEmpty()) {
                val latestMessage = newAssistantMessages.last()
                prefs.lastSeenTimestamp = latestMessage.timestamp
                if (latestMessage.tsIso.isNotBlank()) prefs.lastSeenIso = latestMessage.tsIso
                showNotification(latestMessage.text)
            }
        }

        return Result.success()
    }

    private fun showNotification(messageText: String) {
        val intent = Intent(applicationContext, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        }
        val pendingIntent = PendingIntent.getActivity(
            applicationContext,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val truncatedText = if (messageText.length > 200) {
            messageText.take(200) + "..."
        } else {
            messageText
        }

        val notification = NotificationCompat.Builder(
            applicationContext,
            CcCompanionApp.NOTIFICATION_CHANNEL_ID
        )
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("Claude responded")
            .setContentText(truncatedText)
            .setStyle(NotificationCompat.BigTextStyle().bigText(truncatedText))
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .build()

        try {
            NotificationManagerCompat.from(applicationContext)
                .notify(NOTIFICATION_ID, notification)
        } catch (e: SecurityException) {
            // Permission not granted
        }
    }

    companion object {
        private const val NOTIFICATION_ID = 1001
    }
}
