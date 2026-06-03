package olegcore

// WebSocket-транспорт клиента + X25519/HMAC хендшейк.
// Порт WebSocketClientTransport из core/transport.py.

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"fmt"
	"net/url"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const maxWSMessage = 1 << 22 // как max_size=2**22 в Python

type wsConfig struct {
	url         string
	preshared   []byte
	tlsInsecure bool
	sni         string // ServerName для TLS (SNI), пусто = из host
}

type wsTransport struct {
	cfg       wsConfig
	conn      *websocket.Conn
	sharedKey []byte
	connected bool
	writeMu   sync.Mutex
}

func newWSTransport(cfg wsConfig) *wsTransport {
	return &wsTransport{cfg: cfg}
}

func (t *wsTransport) connect() error {
	u, err := url.Parse(t.cfg.url)
	if err != nil {
		return fmt.Errorf("плохой url: %w", err)
	}

	dialer := *websocket.DefaultDialer
	dialer.HandshakeTimeout = 15 * time.Second
	dialer.ReadBufferSize = 1 << 16
	dialer.WriteBufferSize = 1 << 16
	if u.Scheme == "wss" {
		tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
		if t.cfg.tlsInsecure {
			tlsCfg.InsecureSkipVerify = true
		} else if t.cfg.sni != "" {
			tlsCfg.ServerName = t.cfg.sni
		}
		dialer.TLSClientConfig = tlsCfg
	}

	conn, _, err := dialer.Dial(t.cfg.url, nil)
	if err != nil {
		return fmt.Errorf("ws dial: %w", err)
	}
	conn.SetReadLimit(maxWSMessage)
	t.conn = conn

	// --- X25519 хендшейк ---
	kp, err := generateKeyPair()
	if err != nil {
		conn.Close()
		return err
	}
	if err := t.writeBinary(kp.public[:]); err != nil {
		conn.Close()
		return fmt.Errorf("send pubkey: %w", err)
	}

	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	serverPub, err := t.readBinary()
	if err != nil {
		conn.Close()
		return fmt.Errorf("recv server pubkey: %w", err)
	}
	if len(serverPub) != publicKeySize {
		conn.Close()
		return fmt.Errorf("некорректный публичный ключ сервера: %d байт", len(serverPub))
	}

	dhSecret, err := kp.exchange(serverPub)
	if err != nil {
		conn.Close()
		return err
	}
	t.sharedKey = dhSecret

	// auth_key = HKDF(dh, salt=preshared, info="auth_c")
	authKey := hkdf32(dhSecret, t.cfg.preshared, []byte("auth_c"))
	mac := hmac.New(sha256.New, authKey)
	mac.Write([]byte("handshake-ok"))
	tag := mac.Sum(nil)

	if err := t.writeBinary(tag); err != nil {
		conn.Close()
		return fmt.Errorf("send auth tag: %w", err)
	}

	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	ack, err := t.readBinary()
	if err != nil {
		conn.Close()
		return fmt.Errorf("recv ack: %w", err)
	}
	if string(ack) != "OK" {
		conn.Close()
		return fmt.Errorf("сервер отверг handshake: %q", ack)
	}

	conn.SetReadDeadline(time.Time{}) // снимаем дедлайн
	t.connected = true
	return nil
}

func (t *wsTransport) writeBinary(data []byte) error {
	t.writeMu.Lock()
	defer t.writeMu.Unlock()
	if t.conn == nil {
		return fmt.Errorf("не подключено")
	}
	return t.conn.WriteMessage(websocket.BinaryMessage, data)
}

// readBinary читает один фрейм. Текстовые фреймы тоже принимаем как байты.
func (t *wsTransport) readBinary() ([]byte, error) {
	if t.conn == nil {
		return nil, fmt.Errorf("не подключено")
	}
	_, data, err := t.conn.ReadMessage()
	if err != nil {
		return nil, err
	}
	return data, nil
}

func (t *wsTransport) send(data []byte) error {
	if !t.connected {
		return fmt.Errorf("не подключено")
	}
	return t.writeBinary(data)
}

func (t *wsTransport) recv() ([]byte, error) {
	if !t.connected {
		return nil, fmt.Errorf("не подключено")
	}
	data, err := t.readBinary()
	if err != nil {
		t.connected = false
		return nil, fmt.Errorf("соединение закрыто: %w", err)
	}
	return data, nil
}

func (t *wsTransport) close() {
	t.connected = false
	if t.conn != nil {
		t.conn.Close()
		t.conn = nil
	}
}

func (t *wsTransport) isConnected() bool { return t.connected }
