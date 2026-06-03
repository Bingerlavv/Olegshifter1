package org.olegshifter.client

import olegcore.Client
import olegcore.Logger
import olegcore.Olegcore
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.concurrent.thread

/**
 * Единый владелец Go-клиента на весь процесс. И Activity, и ProxyService
 * подписываются как слушатели. Go-ядро (olegcore.aar) дёргается отсюда.
 */
object Engine {

    enum class Status { DISCONNECTED, CONNECTING, CONNECTED, ERROR }

    interface Listener {
        fun onStatus(status: Status)
        fun onLog(line: String)
    }

    @Volatile
    var status: Status = Status.DISCONNECTED
        private set

    private const val MAX_LOG = 500
    private val logBuffer = ArrayDeque<String>()
    private val listeners = CopyOnWriteArrayList<Listener>()

    private var client: Client? = null

    // Go: Logger { Log(line) }. SAM-конверсия из Java-интерфейса.
    private val goLogger = Logger { line -> appendLog(line) }

    fun addListener(l: Listener) {
        listeners.add(l)
        l.onStatus(status)
    }

    fun removeListener(l: Listener) {
        listeners.remove(l)
    }

    fun snapshotLogs(): List<String> = synchronized(logBuffer) { logBuffer.toList() }

    /** Публичный лог из сервисов/UI. */
    fun appendLogPublic(line: String) = appendLog(line)

    fun connect(cfg: ProxyConfig) {
        if (status == Status.CONNECTING || status == Status.CONNECTED) return
        setStatus(Status.CONNECTING)
        thread(name = "oleg-connect") {
            try {
                val c = Olegcore.newClient()
                c.start(
                    cfg.host,
                    cfg.basePort.toLong(),
                    cfg.channels.toLong(),
                    cfg.socksHost,
                    cfg.socksPort.toLong(),
                    cfg.preshared,
                    cfg.useTls,
                    cfg.tlsInsecure,
                    cfg.sni,
                    goLogger,
                )
                client = c
                setStatus(Status.CONNECTED)
            } catch (e: Exception) {
                appendLog("ОШИБКА: ${e.message}")
                setStatus(Status.ERROR)
            }
        }
    }

    fun disconnect() {
        thread(name = "oleg-disconnect") {
            try {
                client?.stop()
            } catch (_: Exception) {
            }
            client = null
            appendLog("Отключено")
            setStatus(Status.DISCONNECTED)
        }
    }

    fun isConnected(): Boolean = status == Status.CONNECTED

    /** Включить VPN-режим: весь трафик из TUN (fd) через локальный SOCKS5. */
    fun startTun(fd: Int, mtu: Int) {
        client?.startTun(fd.toLong(), mtu.toLong())
        appendLog("VPN-режим включён")
    }

    /** Выключить VPN-режим (туннель остаётся). */
    fun stopTun() {
        try {
            client?.stopTun()
        } catch (_: Exception) {
        }
    }

    private fun setStatus(s: Status) {
        status = s
        listeners.forEach { it.onStatus(s) }
    }

    private fun appendLog(line: String) {
        val ts = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        val entry = "[$ts] $line"
        synchronized(logBuffer) {
            logBuffer.addLast(entry)
            while (logBuffer.size > MAX_LOG) logBuffer.removeFirst()
        }
        listeners.forEach { it.onLog(entry) }
    }
}
