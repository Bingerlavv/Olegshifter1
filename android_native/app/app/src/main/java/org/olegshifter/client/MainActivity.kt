package org.olegshifter.client

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import org.olegshifter.client.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity(), Engine.Listener {

    private lateinit var b: ActivityMainBinding

    private val notifPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* игнорируем отказ */ }

    // Согласие пользователя на VPN.
    private val vpnPrepareLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
            if (res.resultCode == Activity.RESULT_OK) {
                try {
                    OlegVpnService.start(this)
                } catch (t: Throwable) {
                    Engine.appendLogPublic("VPN старт упал: ${t.javaClass.simpleName}: ${t.message}")
                }
            } else {
                Engine.appendLogPublic("VPN: разрешение не выдано")
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)

        loadIntoUi(Prefs.load(this))
        maybeRequestNotifPermission()

        b.connectButton.setOnClickListener {
            try {
                if (Engine.isConnected() || Engine.status == Engine.Status.CONNECTING) {
                    // Гасим тот сервис, что был запущен (режим запомнен в Prefs).
                    if (Prefs.load(this).vpnMode) OlegVpnService.stop(this)
                    else ProxyService.stop(this)
                } else {
                    val cfg = readFromUi()
                    Prefs.save(this, cfg)
                    if (cfg.vpnMode) prepareAndStartVpn() else ProxyService.start(this)
                }
            } catch (t: Throwable) {
                Engine.appendLogPublic("Ошибка запуска: ${t.javaClass.simpleName}: ${t.message}")
            }
        }

        // Восстанавливаем накопленный лог.
        b.logView.text = Engine.snapshotLogs().joinToString("\n")
    }

    override fun onStart() {
        super.onStart()
        Engine.addListener(this)
    }

    override fun onStop() {
        Engine.removeListener(this)
        super.onStop()
    }

    // --- Engine.Listener (приходит из фонового потока) ---

    override fun onStatus(status: Engine.Status) = runOnUiThread {
        val (text, color) = when (status) {
            Engine.Status.CONNECTED -> getString(R.string.status_connected) to R.color.status_green
            Engine.Status.CONNECTING -> getString(R.string.status_connecting) to R.color.status_orange
            Engine.Status.ERROR -> getString(R.string.status_error) to R.color.status_red
            Engine.Status.DISCONNECTED -> getString(R.string.status_disconnected) to R.color.status_red
        }
        b.statusLabel.text = text
        b.statusLabel.setTextColor(ContextCompat.getColor(this, color))

        val connectingOrUp = status == Engine.Status.CONNECTED || status == Engine.Status.CONNECTING
        b.connectButton.text =
            getString(if (connectingOrUp) R.string.disconnect else R.string.connect)
        setInputsEnabled(!connectingOrUp)
    }

    override fun onLog(line: String) = runOnUiThread {
        b.logView.append("\n$line")
    }

    // --- UI <-> config ---

    private fun loadIntoUi(c: ProxyConfig) {
        b.hostInput.setText(c.host)
        b.basePortInput.setText(c.basePort.toString())
        b.channelsInput.setText(c.channels.toString())
        b.socksPortInput.setText(c.socksPort.toString())
        b.presharedInput.setText(c.preshared)
        b.tlsSwitch.isChecked = c.useTls
        b.insecureSwitch.isChecked = c.tlsInsecure
        b.vpnSwitch.isChecked = c.vpnMode
    }

    private fun prepareAndStartVpn() {
        val intent = VpnService.prepare(this)
        if (intent != null) vpnPrepareLauncher.launch(intent) else OlegVpnService.start(this)
    }

    private fun readFromUi(): ProxyConfig = ProxyConfig(
        host = b.hostInput.text.toString().trim(),
        basePort = b.basePortInput.text.toString().toIntOrNull() ?: 8443,
        channels = b.channelsInput.text.toString().toIntOrNull()?.coerceAtLeast(1) ?: 3,
        socksHost = "127.0.0.1",
        socksPort = b.socksPortInput.text.toString().toIntOrNull() ?: 1080,
        preshared = b.presharedInput.text.toString(),
        useTls = b.tlsSwitch.isChecked,
        tlsInsecure = b.insecureSwitch.isChecked,
        sni = "",
        vpnMode = b.vpnSwitch.isChecked,
    )

    private fun setInputsEnabled(enabled: Boolean) {
        b.hostInput.isEnabled = enabled
        b.basePortInput.isEnabled = enabled
        b.channelsInput.isEnabled = enabled
        b.socksPortInput.isEnabled = enabled
        b.presharedInput.isEnabled = enabled
        b.tlsSwitch.isEnabled = enabled
        b.insecureSwitch.isEnabled = enabled
        b.vpnSwitch.isEnabled = enabled
    }

    private fun maybeRequestNotifPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}
