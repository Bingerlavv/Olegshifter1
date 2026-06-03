package org.olegshifter.client

import android.content.Context

/** Параметры подключения. Совпадают по смыслу с examples/shared.py. */
data class ProxyConfig(
    val host: String,
    val basePort: Int,
    val channels: Int,
    val socksHost: String,
    val socksPort: Int,
    val preshared: String,
    val useTls: Boolean,
    val tlsInsecure: Boolean,
    val sni: String,
    val vpnMode: Boolean,
)

/** Сохранение/загрузка конфига в SharedPreferences. */
object Prefs {
    private const val FILE = "oleg_prefs"

    fun save(ctx: Context, c: ProxyConfig) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().apply {
            putString("host", c.host)
            putInt("basePort", c.basePort)
            putInt("channels", c.channels)
            putString("socksHost", c.socksHost)
            putInt("socksPort", c.socksPort)
            putString("preshared", c.preshared)
            putBoolean("useTls", c.useTls)
            putBoolean("tlsInsecure", c.tlsInsecure)
            putString("sni", c.sni)
            putBoolean("vpnMode", c.vpnMode)
            apply()
        }
    }

    fun load(ctx: Context): ProxyConfig {
        val p = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        return ProxyConfig(
            host = p.getString("host", "127.0.0.1") ?: "127.0.0.1",
            basePort = p.getInt("basePort", 8443),
            channels = p.getInt("channels", 3),
            socksHost = p.getString("socksHost", "127.0.0.1") ?: "127.0.0.1",
            socksPort = p.getInt("socksPort", 1080),
            preshared = p.getString("preshared", "CHANGE-ME-32-bytes-shared-secret")
                ?: "CHANGE-ME-32-bytes-shared-secret",
            useTls = p.getBoolean("useTls", true),
            tlsInsecure = p.getBoolean("tlsInsecure", false),
            sni = p.getString("sni", "") ?: "",
            vpnMode = p.getBoolean("vpnMode", true),
        )
    }
}
