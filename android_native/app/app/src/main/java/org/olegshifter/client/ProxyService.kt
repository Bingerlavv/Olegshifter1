package org.olegshifter.client

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * Foreground-сервис: держит процесс живым, пока поднят SOCKS5-туннель,
 * и показывает статус в шторке. Сама логика — в [Engine].
 */
class ProxyService : Service(), Engine.Listener {

    companion object {
        const val ACTION_START = "org.olegshifter.client.START"
        const val ACTION_STOP = "org.olegshifter.client.STOP"
        private const val CHANNEL_ID = "oleg_proxy"
        private const val NOTIF_ID = 42

        fun start(ctx: Context) {
            val i = Intent(ctx, ProxyService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i)
            } else {
                ctx.startService(i)
            }
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, ProxyService::class.java).setAction(ACTION_STOP))
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
        Engine.addListener(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                Engine.disconnect()
                stopForegroundCompat()
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                try {
                    startForeground(NOTIF_ID, buildNotification(getString(R.string.status_connecting)))
                } catch (t: Throwable) {
                    Engine.appendLogPublic("startForeground упал: ${t.javaClass.simpleName}: ${t.message}")
                }
                Engine.connect(Prefs.load(this))
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        Engine.removeListener(this)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // --- Engine.Listener ---

    override fun onStatus(status: Engine.Status) {
        when (status) {
            Engine.Status.CONNECTED -> {
                val cfg = Prefs.load(this)
                updateNotification("${getString(R.string.status_connected)} • SOCKS5 ${cfg.socksHost}:${cfg.socksPort}")
            }
            Engine.Status.CONNECTING -> updateNotification(getString(R.string.status_connecting))
            Engine.Status.ERROR -> updateNotification(getString(R.string.status_error))
            Engine.Status.DISCONNECTED -> {
                stopForegroundCompat()
                stopSelf()
            }
        }
    }

    override fun onLog(line: String) { /* лог показывает Activity */ }

    // --- notifications ---

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.channel_name),
                NotificationManager.IMPORTANCE_LOW,
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
    }

    private fun buildNotification(text: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            this, 1,
            Intent(this, ProxyService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_shield)
            .setContentIntent(openIntent)
            .setOngoing(true)
            .addAction(0, getString(R.string.disconnect), stopIntent)
            .build()
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIF_ID, buildNotification(text))
    }

    @Suppress("DEPRECATION")
    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            stopForeground(true)
        }
    }
}
