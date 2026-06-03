package org.olegshifter.client

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import androidx.core.app.NotificationCompat

/**
 * Системный VPN: весь трафик телефона заворачивается в TUN, gvisor-стек в Go
 * терминирует его и гонит через локальный SOCKS5 → туннель OlegShifter.
 *
 * Наш SOCKS5 — TCP-only, поэтому:
 *   - TCP идёт через туннель,
 *   - DNS (UDP:53) резолвится DNS-over-TCP внутри Go,
 *   - прочий UDP (QUIC) отбрасывается → приложения откатываются на TCP.
 */
class OlegVpnService : VpnService(), Engine.Listener {

    companion object {
        const val ACTION_START = "org.olegshifter.client.VPN_START"
        const val ACTION_STOP = "org.olegshifter.client.VPN_STOP"
        private const val CHANNEL_ID = "oleg_vpn"
        private const val NOTIF_ID = 43
        private const val MTU = 1500
        private const val VPN_ADDR = "10.0.0.2"
        private const val DNS = "1.1.1.1"

        fun start(ctx: Context) {
            val i = Intent(ctx, OlegVpnService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i)
            else ctx.startService(i)
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, OlegVpnService::class.java).setAction(ACTION_STOP))
        }
    }

    private var tunInterface: ParcelFileDescriptor? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        Engine.addListener(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                shutdown()
                return START_NOT_STICKY
            }
            else -> {
                startForeground(NOTIF_ID, buildNotification(getString(R.string.status_connecting)))
                // Поднимаем туннель; TUN установим, когда статус станет CONNECTED.
                Engine.connect(Prefs.load(this))
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        Engine.removeListener(this)
        super.onDestroy()
    }

    override fun onRevoke() {
        // Система отозвала VPN (другой VPN включился) — корректно гасим.
        shutdown()
        super.onRevoke()
    }

    // --- Engine.Listener ---

    override fun onStatus(status: Engine.Status) {
        when (status) {
            Engine.Status.CONNECTED -> {
                if (tunInterface == null) establishTun()
                updateNotification(getString(R.string.status_connected) + " • VPN")
            }
            Engine.Status.CONNECTING -> updateNotification(getString(R.string.status_connecting))
            Engine.Status.ERROR -> {
                updateNotification(getString(R.string.status_error))
                shutdown()
            }
            Engine.Status.DISCONNECTED -> shutdown()
        }
    }

    override fun onLog(line: String) { /* лог показывает Activity */ }

    // --- TUN ---

    private fun establishTun() {
        try {
            val builder = Builder()
                .setSession(getString(R.string.app_name))
                .setMtu(MTU)
                .addAddress(VPN_ADDR, 32)
                .addRoute("0.0.0.0", 0)
                .addDnsServer(DNS)

            // Исключаем себя из VPN, иначе исходящие SOCKS5/WS зациклятся в TUN.
            try {
                builder.addDisallowedApplication(packageName)
            } catch (_: PackageManager.NameNotFoundException) {
            }

            val pfd = builder.establish() ?: run {
                Engine.appendLogPublic("VPN: establish() вернул null (нет разрешения?)")
                shutdown()
                return
            }
            tunInterface = pfd
            Engine.startTun(pfd.fd, MTU)
        } catch (e: Exception) {
            Engine.appendLogPublic("VPN ошибка: ${e.message}")
            shutdown()
        }
    }

    private fun shutdown() {
        Engine.stopTun()
        try {
            tunInterface?.close()
        } catch (_: Exception) {
        }
        tunInterface = null
        Engine.disconnect()
        stopForegroundCompat()
        stopSelf()
    }

    // --- notifications ---

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL_ID, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW,
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
    }

    private fun buildNotification(text: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            this, 1, Intent(this, OlegVpnService::class.java).setAction(ACTION_STOP),
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
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
    }

    @Suppress("DEPRECATION")
    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) stopForeground(STOP_FOREGROUND_REMOVE)
        else stopForeground(true)
    }
}
