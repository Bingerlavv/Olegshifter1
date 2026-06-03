// Package olegcore — клиентское ядро OlegShifter для Android (gomobile) и десктопа.
//
// Публичный API (виден из Kotlin после `gomobile bind`):
//
//	c := olegcore.NewClient()
//	err := c.Start(host, basePort, channels, "127.0.0.1", 1080, preshared, useTLS, tlsInsecure, sni, logger)
//	...
//	c.Stop()
//
// Start поднимает `channels` WebSocket-каналов, проходит X25519/HMAC хендшейк,
// выводит единый сессионный ключ и запускает локальный SOCKS5 на socksHost:socksPort.
package olegcore

import (
	"fmt"
	"net"
	"strconv"
	"sync"
)

// Logger — колбэк логов в UI. gomobile сгенерирует Java/Kotlin интерфейс.
type Logger interface {
	Log(line string)
}

// recoverLogf перехватывает panic в горутине и пишет её в лог вместо краша
// всего приложения (Go-паника в нативной либе иначе убивает процесс).
func recoverLogf(logf func(string), where string) {
	if r := recover(); r != nil && logf != nil {
		logf(fmt.Sprintf("PANIC в %s: %v", where, r))
	}
}

// tunController управляет VPN-мостом (реализация платформозависима).
type tunController interface {
	stop()
}

// Client — управляемый экземпляр клиента.
type Client struct {
	mu      sync.Mutex
	mgr     *manager
	socks   *socks5Server
	running bool

	// для VPN-режима
	socksHost string
	socksPort int
	logf      func(string)
	tun       tunController
}

// NewClient создаёт новый (ещё не запущенный) клиент.
func NewClient() *Client { return &Client{} }

// Start подключает каналы и поднимает SOCKS5. Блокирует до готовности либо ошибки.
//
//	host         — адрес сервера (IP или домен)
//	basePort     — первый WS-порт; канал ch использует basePort+ch
//	channels     — сколько WS-каналов поднять (>=1)
//	socksHost    — где слушать SOCKS5 (обычно 127.0.0.1)
//	socksPort    — порт SOCKS5 (обычно 1080)
//	preshared    — общий секрет (как на сервере; байты = UTF-8 строки)
//	useTLS       — true → wss://, false → ws://
//	tlsInsecure  — не проверять сертификат (dev/self-signed)
//	sni          — SNI/ServerName для TLS (пусто = из host)
//	logger       — приёмник логов (может быть nil)
func (c *Client) Start(
	host string, basePort int, channels int,
	socksHost string, socksPort int,
	preshared string, useTLS bool, tlsInsecure bool, sni string,
	logger Logger,
) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("panic в Start: %v", r)
		}
	}()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.running {
		return fmt.Errorf("уже запущен")
	}
	if channels < 1 {
		channels = 1
	}

	logf := func(s string) {
		if logger != nil {
			logger.Log(s)
		}
	}
	c.logf = logf
	c.socksHost = socksHost
	c.socksPort = socksPort

	scheme := "ws"
	if useTLS {
		scheme = "wss"
	}
	presharedBytes := []byte(preshared)

	transports := make(map[int]*wsTransport)
	configs := make(map[int]wsConfig)
	sharedKeys := make([][]byte, 0, channels)

	cleanup := func() {
		for _, t := range transports {
			t.close()
		}
	}

	for ch := 0; ch < channels; ch++ {
		cfg := wsConfig{
			url:         fmt.Sprintf("%s://%s:%d/ws", scheme, host, basePort+ch),
			preshared:   presharedBytes,
			tlsInsecure: tlsInsecure,
			sni:         sni,
		}
		t := newWSTransport(cfg)
		logf(fmt.Sprintf("Подключаю WS канал %d → %s ...", ch, cfg.url))
		if err := t.connect(); err != nil {
			cleanup()
			return fmt.Errorf("WS канал %d не подключился: %w", ch, err)
		}
		if len(t.sharedKey) != 32 {
			cleanup()
			return fmt.Errorf("канал %d: нет shared_key после handshake", ch)
		}
		transports[ch] = t
		configs[ch] = cfg
		sharedKeys = append(sharedKeys, t.sharedKey) // ch идёт по порядку 0..n
		logf(fmt.Sprintf("✓ WS канал %d подключён", ch))
	}

	// Единый сессионный ключ из shared_key всех каналов (как examples/shared.py).
	c2s, s2c := deriveSessionKeys(sharedKeys, presharedBytes)
	crypto := newCryptoSession(c2s, s2c) // клиент: send=c2s, recv=s2c

	c.mgr = newManager(crypto, transports, configs, logf)
	c.mgr.start()

	c.socks = newSocks5Server(c.mgr, socksHost, socksPort)
	if err := c.socks.start(); err != nil {
		c.mgr.stop()
		c.mgr = nil
		return fmt.Errorf("SOCKS5 не запустился: %w", err)
	}

	c.running = true
	logf("============================================================")
	logf(fmt.Sprintf("ГОТОВО! Каналов: %d WS", channels))
	logf(fmt.Sprintf("SOCKS5 прокси: %s:%d", socksHost, socksPort))
	logf("============================================================")
	return nil
}

// StartTun включает VPN-режим: весь трафic из TUN-устройства (fd от Android
// VpnService) идёт через локальный SOCKS5. Вызывать после Start().
// Только Android: на десктопе вернёт ошибку.
func (c *Client) StartTun(fd int, mtu int) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("panic в StartTun: %v", r)
		}
	}()
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.running {
		return fmt.Errorf("сначала вызови Start()")
	}
	if c.tun != nil {
		return fmt.Errorf("VPN уже запущен")
	}
	socksAddr := net.JoinHostPort(c.socksHost, strconv.Itoa(c.socksPort))
	t, err := startTunBridge(fd, mtu, socksAddr, c.logf)
	if err != nil {
		return err
	}
	c.tun = t
	return nil
}

// StopTun выключает VPN-режим (туннель и SOCKS5 продолжают работать).
func (c *Client) StopTun() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.tun != nil {
		c.tun.stop()
		c.tun = nil
	}
}

// Stop останавливает SOCKS5 и все каналы.
func (c *Client) Stop() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.running {
		return
	}
	if c.tun != nil {
		c.tun.stop()
		c.tun = nil
	}
	if c.socks != nil {
		c.socks.stop()
		c.socks = nil
	}
	if c.mgr != nil {
		c.mgr.stop()
		c.mgr = nil
	}
	c.running = false
}

// IsRunning — запущен ли клиент.
func (c *Client) IsRunning() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.running
}
