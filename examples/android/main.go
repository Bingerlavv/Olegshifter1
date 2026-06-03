package main

// OlegShifter — Go/Fyne клиент, совместимый с Python-сервером.
//
// Стек (идентично Python):
//   SOCKS5 → ChannelManager → Padder → CryptoSession → WSTransport → Server
//
// Handshake (идентично transport.py):
//   client ephemeral X25519 pub(32) →
//   ← server ephemeral X25519 pub(32)
//   HMAC-SHA256(HKDF(dh, preshared, "auth_c", "auth_s"), "handshake-ok")(32) →
//   ← b"OK"
//   shared_key = dh_secret (32 байта)
//
// CryptoSession (идентично crypto_core.py):
//   encrypt: nonce(12) || header_ct(8+16) || body_ct(len+16)
//   header = stream_id(4 BE) || seq(4 BE), шифруется ChaCha20-Poly1305
//
// Chunk (идентично multipath.py):
//   magic(2=0xABCD) || stream_id(4) || seq(8) || flags(1) || length(2) || channel(1) || payload
//
// Padding (идентично padding.py, BIMODAL):
//   type(1) || real_len(2) || data || random_padding
//
// Зависимости (go.mod):
//   fyne.io/fyne/v2
//   github.com/gorilla/websocket
//   golang.org/x/crypto
//
// Сборка APK:
//   go install fyne.io/fyne/v2/cmd/fyne@latest
//   fyne package -os android -appID com.olegshifter.proxy -name OlegShifter

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/binary"
	"fmt"
	"image/color"
	"io"
	"math"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
	"github.com/gorilla/websocket"
	"golang.org/x/crypto/chacha20poly1305"
	"golang.org/x/crypto/curve25519"
	"golang.org/x/crypto/hkdf"
)

// ─────────────────────────────────────────────────────────────────────────────
// Тема
// ─────────────────────────────────────────────────────────────────────────────

var (
	colorBg       = color.NRGBA{R: 13, G: 17, B: 23, A: 255}
	colorCard     = color.NRGBA{R: 22, G: 27, B: 34, A: 255}
	colorBorder   = color.NRGBA{R: 48, G: 54, B: 61, A: 255}
	colorAccent   = color.NRGBA{R: 88, G: 166, B: 255, A: 255}
	colorGreen    = color.NRGBA{R: 63, G: 185, B: 80, A: 255}
	colorRed      = color.NRGBA{R: 248, G: 81, B: 73, A: 255}
	colorYellow   = color.NRGBA{R: 210, G: 153, B: 34, A: 255}
	colorTextMain = color.NRGBA{R: 230, G: 237, B: 243, A: 255}
	colorTextSub  = color.NRGBA{R: 125, G: 133, B: 144, A: 255}
)

type darkTheme struct{}

func (darkTheme) Color(n fyne.ThemeColorName, _ fyne.ThemeVariant) color.Color {
	switch n {
	case theme.ColorNameBackground:
		return colorBg
	case theme.ColorNameButton:
		return colorCard
	case theme.ColorNameDisabledButton:
		return colorBorder
	case theme.ColorNameForeground:
		return colorTextMain
	case theme.ColorNamePlaceHolder:
		return colorTextSub
	case theme.ColorNameInputBackground:
		return colorCard
	case theme.ColorNamePrimary, theme.ColorNameFocus:
		return colorAccent
	case theme.ColorNameShadow:
		return color.NRGBA{A: 80}
	case theme.ColorNameSeparator:
		return colorBorder
	}
	return theme.DefaultTheme().Color(n, 0)
}
func (darkTheme) Font(s fyne.TextStyle) fyne.Resource     { return theme.DefaultTheme().Font(s) }
func (darkTheme) Icon(n fyne.ThemeIconName) fyne.Resource  { return theme.DefaultTheme().Icon(n) }
func (darkTheme) Size(n fyne.ThemeSizeName) float32 {
	switch n {
	case theme.SizeNamePadding:
		return 8
	case theme.SizeNameInnerPadding:
		return 10
	case theme.SizeNameText:
		return 13
	case theme.SizeNameInputBorder:
		return 1
	}
	return theme.DefaultTheme().Size(n)
}

// ─────────────────────────────────────────────────────────────────────────────
// UI виджеты
// ─────────────────────────────────────────────────────────────────────────────

type StatusDot struct {
	widget.BaseWidget
	col color.Color
}

func NewStatusDot(c color.Color) *StatusDot {
	d := &StatusDot{col: c}
	d.ExtendBaseWidget(d)
	return d
}
func (d *StatusDot) SetColor(c color.Color) { d.col = c; d.Refresh() }
func (d *StatusDot) CreateRenderer() fyne.WidgetRenderer {
	circ := canvas.NewCircle(d.col)
	return &dotR{d: d, c: circ}
}

type dotR struct {
	d *StatusDot
	c *canvas.Circle
}

func (r *dotR) Layout(s fyne.Size) {
	r.c.Resize(fyne.NewSize(10, 10))
	r.c.Move(fyne.NewPos((s.Width-10)/2, (s.Height-10)/2))
}
func (r *dotR) MinSize() fyne.Size           { return fyne.NewSize(14, 14) }
func (r *dotR) Refresh()                     { r.c.FillColor = r.d.col; r.c.Refresh() }
func (r *dotR) Destroy()                     {}
func (r *dotR) Objects() []fyne.CanvasObject { return []fyne.CanvasObject{r.c} }

type ChannelCard struct {
	widget.BaseWidget
	dot   *StatusDot
	label *widget.Label
	sub   *widget.Label
}

func NewChannelCard(i int) *ChannelCard {
	c := &ChannelCard{
		dot:   NewStatusDot(colorTextSub),
		label: widget.NewLabel(fmt.Sprintf("Ch %d", i+1)),
		sub:   widget.NewLabel("Idle"),
	}
	c.label.TextStyle = fyne.TextStyle{Bold: true}
	c.sub.Importance = widget.LowImportance
	c.ExtendBaseWidget(c)
	return c
}
func (c *ChannelCard) SetStatus(s string) {
	switch s {
	case "connecting":
		c.dot.SetColor(colorYellow)
		c.sub.SetText("Connecting…")
	case "connected":
		c.dot.SetColor(colorGreen)
		c.sub.SetText("Connected")
	case "error":
		c.dot.SetColor(colorRed)
		c.sub.SetText("Error")
	default:
		c.dot.SetColor(colorTextSub)
		c.sub.SetText("Idle")
	}
}
func (c *ChannelCard) CreateRenderer() fyne.WidgetRenderer {
	bg := canvas.NewRectangle(colorCard)
	bg.CornerRadius = 8
	bd := canvas.NewRectangle(color.Transparent)
	bd.StrokeColor = colorBorder
	bd.StrokeWidth = 1
	bd.CornerRadius = 8
	cnt := container.NewPadded(container.NewBorder(nil, nil, container.NewCenter(c.dot), nil, container.NewVBox(c.label, c.sub)))
	return &cardR{bg: bg, bd: bd, cnt: cnt}
}

type cardR struct {
	bg, bd *canvas.Rectangle
	cnt    fyne.CanvasObject
}

func (r *cardR) Layout(s fyne.Size)           { r.bg.Resize(s); r.bd.Resize(s); r.cnt.Resize(s) }
func (r *cardR) MinSize() fyne.Size           { return fyne.NewSize(90, 56) }
func (r *cardR) Refresh()                     { r.bg.Refresh(); r.bd.Refresh(); r.cnt.Refresh() }
func (r *cardR) Destroy()                     {}
func (r *cardR) Objects() []fyne.CanvasObject { return []fyne.CanvasObject{r.bg, r.bd, r.cnt} }

func logLine(ts, msg string, col color.Color) fyne.CanvasObject {
	t := canvas.NewText(ts+"  ", colorTextSub)
	t.TextSize = 11
	m := canvas.NewText(msg, col)
	m.TextSize = 12
	return container.NewHBox(t, m)
}
func secLabel(s string) *canvas.Text {
	t := canvas.NewText(s, colorTextSub)
	t.TextSize = 11
	t.TextStyle = fyne.TextStyle{Bold: true}
	return t
}
func sep() fyne.CanvasObject {
	r := canvas.NewRectangle(colorBorder)
	r.SetMinSize(fyne.NewSize(0, 1))
	return r
}
func humanBytes(n int64) string {
	switch {
	case n >= 1<<30:
		return fmt.Sprintf("%.1f GB", float64(n)/float64(1<<30))
	case n >= 1<<20:
		return fmt.Sprintf("%.1f MB", float64(n)/float64(1<<20))
	case n >= 1<<10:
		return fmt.Sprintf("%.1f KB", float64(n)/float64(1<<10))
	default:
		return fmt.Sprintf("%d B", n)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Crypto (совместимо с crypto_core.py)
// ─────────────────────────────────────────────────────────────────────────────

// deriveKeys — HKDF-SHA256, возвращает два 32-байтных ключа
func deriveKeys(secret, salt, infoC, infoS []byte) (c2s, s2c [32]byte, err error) {
	derive := func(info []byte) ([32]byte, error) {
		var out [32]byte
		r := hkdf.New(sha256.New, secret, salt, info)
		_, e := io.ReadFull(r, out[:])
		return out, e
	}
	c2s, err = derive(infoC)
	if err != nil {
		return
	}
	s2c, err = derive(infoS)
	return
}

// makeNonce: 4 нулевых байта + uint64(seq) BE (как в crypto_core.py)
func makeNonce(seq uint32) []byte {
	n := make([]byte, 12)
	binary.BigEndian.PutUint64(n[4:], uint64(seq))
	return n
}

// CryptoSession — ChaCha20-Poly1305, совместимо с crypto_core.py
type CryptoSession struct {
	mu      sync.Mutex
	sendKey [32]byte
	recvKey [32]byte
	sendSeq uint32
}

func NewCryptoSession(sendKey, recvKey [32]byte) *CryptoSession {
	return &CryptoSession{sendKey: sendKey, recvKey: recvKey}
}

// Encrypt → nonce(12) || header_ct(24) || body_ct(len+16)
func (cs *CryptoSession) Encrypt(streamID uint32, plaintext []byte) ([]byte, error) {
	cs.mu.Lock()
	seq := cs.sendSeq
	cs.sendSeq++
	key := cs.sendKey
	cs.mu.Unlock()

	nonce := makeNonce(seq)
	aead, err := chacha20poly1305.New(key[:])
	if err != nil {
		return nil, err
	}

	header := make([]byte, 8)
	binary.BigEndian.PutUint32(header[0:4], streamID)
	binary.BigEndian.PutUint32(header[4:8], seq)

	headerCT := aead.Seal(nil, nonce, header, nonce)   // AAD = nonce
	bodyCT := aead.Seal(nil, nonce, plaintext, headerCT) // AAD = headerCT

	out := make([]byte, 0, 12+len(headerCT)+len(bodyCT))
	out = append(out, nonce...)
	out = append(out, headerCT...)
	out = append(out, bodyCT...)
	return out, nil
}

// Decrypt → (streamID, seq, plaintext)
func (cs *CryptoSession) Decrypt(packet []byte) (uint32, uint32, []byte, error) {
	const headerEncSz = 8 + 16
	if len(packet) < 12+headerEncSz+16 {
		return 0, 0, nil, fmt.Errorf("packet too short: %d", len(packet))
	}
	nonce := packet[:12]
	headerCT := packet[12 : 12+headerEncSz]
	bodyCT := packet[12+headerEncSz:]

	cs.mu.Lock()
	key := cs.recvKey
	cs.mu.Unlock()

	aead, err := chacha20poly1305.New(key[:])
	if err != nil {
		return 0, 0, nil, err
	}
	header, err := aead.Open(nil, nonce, headerCT, nonce)
	if err != nil {
		return 0, 0, nil, fmt.Errorf("header decrypt: %w", err)
	}
	plaintext, err := aead.Open(nil, nonce, bodyCT, headerCT)
	if err != nil {
		return 0, 0, nil, fmt.Errorf("body decrypt: %w", err)
	}
	sid := binary.BigEndian.Uint32(header[0:4])
	seq := binary.BigEndian.Uint32(header[4:8])
	return sid, seq, plaintext, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Chunk framing (совместимо с multipath.py)
// ─────────────────────────────────────────────────────────────────────────────

var chunkMagic = [2]byte{0xAB, 0xCD}

const (
	chunkHeaderSize = 18 // magic(2)+stream(4)+seq(8)+flags(1)+length(2)+channel(1)
	flagData        = byte(0x01)
	flagFIN         = byte(0x02)
	flagPing        = byte(0x04)
)

type Chunk struct {
	StreamID uint32
	Seq      uint64
	Flags    byte
	Channel  byte
	Payload  []byte
}

func (c *Chunk) Pack() []byte {
	buf := make([]byte, chunkHeaderSize+len(c.Payload))
	buf[0] = chunkMagic[0]
	buf[1] = chunkMagic[1]
	binary.BigEndian.PutUint32(buf[2:6], c.StreamID)
	binary.BigEndian.PutUint64(buf[6:14], c.Seq)
	buf[14] = c.Flags
	binary.BigEndian.PutUint16(buf[15:17], uint16(len(c.Payload)))
	buf[17] = c.Channel
	copy(buf[18:], c.Payload)
	return buf
}

func UnpackChunk(data []byte) (*Chunk, error) {
	if len(data) < chunkHeaderSize {
		return nil, fmt.Errorf("chunk too short: %d", len(data))
	}
	if data[0] != chunkMagic[0] || data[1] != chunkMagic[1] {
		return nil, fmt.Errorf("bad magic: %02x%02x", data[0], data[1])
	}
	length := int(binary.BigEndian.Uint16(data[15:17]))
	if chunkHeaderSize+length > len(data) {
		return nil, fmt.Errorf("chunk payload truncated")
	}
	payload := make([]byte, length)
	copy(payload, data[chunkHeaderSize:chunkHeaderSize+length])
	return &Chunk{
		StreamID: binary.BigEndian.Uint32(data[2:6]),
		Seq:      binary.BigEndian.Uint64(data[6:14]),
		Flags:    data[14],
		Channel:  data[17],
		Payload:  payload,
	}, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Padding (совместимо с padding.py, BIMODAL)
// ─────────────────────────────────────────────────────────────────────────────

const (
	pktTypeData  = byte(0x01)
	pktTypeChaff = byte(0x02)
	paddingHdrSz = 3
)

func randFloat() float64 {
	var b [4]byte
	rand.Read(b[:])
	return float64(binary.BigEndian.Uint32(b[:])) / float64(1<<32)
}

func gaussRand(mean, sigma float64) float64 {
	u1 := math.Max(randFloat(), 1e-10)
	u2 := randFloat()
	z := math.Sqrt(-2*math.Log(u1)) * math.Cos(2*math.Pi*u2)
	return mean + sigma*z
}

func padData(data []byte) []byte {
	const (
		minSz     = 64
		maxSz     = 1400
		peakSmall = 128.0
		peakLarge = 1200.0
		peakRatio = 0.3
	)
	minimum := paddingHdrSz + len(data)
	if minimum < minSz {
		minimum = minSz
	}
	var target int
	if randFloat() < peakRatio {
		target = int(gaussRand(peakSmall, peakSmall*0.15))
	} else {
		target = int(gaussRand(peakLarge, peakLarge*0.08))
	}
	if target < minimum {
		target = minimum
	}
	if target > maxSz {
		target = maxSz
	}
	padLen := target - paddingHdrSz - len(data)
	if padLen < 0 {
		padLen = 0
	}
	buf := make([]byte, paddingHdrSz+len(data)+padLen)
	buf[0] = pktTypeData
	binary.BigEndian.PutUint16(buf[1:3], uint16(len(data)))
	copy(buf[3:], data)
	rand.Read(buf[3+len(data):])
	return buf
}

func unpadData(packet []byte) ([]byte, error) {
	if len(packet) < paddingHdrSz {
		return nil, fmt.Errorf("too short")
	}
	pktType := packet[0]
	realLen := int(binary.BigEndian.Uint16(packet[1:3]))
	if pktType == pktTypeChaff {
		return nil, nil // chaff — выбрасываем
	}
	if pktType != pktTypeData {
		return nil, fmt.Errorf("unknown type: %d", pktType)
	}
	if paddingHdrSz+realLen > len(packet) {
		return nil, fmt.Errorf("real_len overflow")
	}
	return packet[paddingHdrSz : paddingHdrSz+realLen], nil
}

// ─────────────────────────────────────────────────────────────────────────────
// WSTransport — X25519 handshake совместимо с transport.py
// ─────────────────────────────────────────────────────────────────────────────

type WSTransport struct {
	id        int
	cfg       Config
	mu        sync.Mutex
	conn      *websocket.Conn
	closed    bool
	SharedKey []byte // DH-секрет (32 байта)

	writeCh   chan []byte
	recvCh    chan []byte
	done      chan struct{}

	bytesSent int64
	bytesRecv int64
}

func NewWSTransport(id int, cfg Config) *WSTransport {
	return &WSTransport{
		id:      id,
		cfg:     cfg,
		writeCh: make(chan []byte, 256),
		recvCh:  make(chan []byte, 256),
		done:    make(chan struct{}),
	}
}

func (t *WSTransport) Connect() error {
	scheme := "ws"
	if t.cfg.UseTLS {
		scheme = "wss"
	}
	// Каждый канал на отдельном порту: BASE_PORT + id (как в Python client.py)
	url := fmt.Sprintf("%s://%s:%d/ws", scheme, t.cfg.ServerHost, t.cfg.ServerPort+t.id)

	dialer := websocket.Dialer{HandshakeTimeout: 15 * time.Second}
	if t.cfg.UseTLS && t.cfg.TLSInsecure {
		dialer.TLSClientConfig = &tls.Config{InsecureSkipVerify: true}
	}

	conn, _, err := dialer.Dial(url, nil)
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}

	// --- X25519 Handshake (transport.py WebSocketClientTransport.connect) ---

	// Генерируем эфемерный ключ (X25519, совместимо с Python cryptography.X25519PrivateKey)
	privKey := make([]byte, 32)
	if _, err = rand.Read(privKey); err != nil {
		conn.Close()
		return err
	}
	pubKey, err := curve25519.X25519(privKey, curve25519.Basepoint)
	if err != nil {
		conn.Close()
		return fmt.Errorf("gen pubkey: %w", err)
	}

	// 1. Отправляем наш pub(32)
	if err = conn.WriteMessage(websocket.BinaryMessage, pubKey); err != nil {
		conn.Close()
		return fmt.Errorf("send pub: %w", err)
	}

	// 2. Получаем pub сервера(32)
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, serverPub, err := conn.ReadMessage()
	if err != nil {
		conn.Close()
		return fmt.Errorf("recv server pub: %w", err)
	}
	if len(serverPub) != 32 {
		conn.Close()
		return fmt.Errorf("bad server pub len: %d", len(serverPub))
	}

	// 3. DH: X25519(privKey, serverPub)
	dhSecret, err := curve25519.X25519(privKey, serverPub)
	if err != nil {
		conn.Close()
		return fmt.Errorf("dh: %w", err)
	}

	// 4. HKDF(dh, preshared, "auth_c", "auth_s") → auth_key (c2s)
	authKey, _, err := deriveKeys(dhSecret, t.cfg.Preshared, []byte("auth_c"), []byte("auth_s"))
	if err != nil {
		conn.Close()
		return err
	}

	// 5. HMAC-SHA256(auth_key, "handshake-ok")
	// Python: hmac.new(auth_key, b"handshake-ok", hashlib.sha256).digest()
	// auth_key — первые 32 байта из HKDF, используем как ключ HMAC напрямую
	macH := hmac.New(sha256.New, authKey[:])
	macH.Write([]byte("handshake-ok"))
	tag := macH.Sum(nil)

	if err = conn.WriteMessage(websocket.BinaryMessage, tag); err != nil {
		conn.Close()
		return fmt.Errorf("send tag: %w", err)
	}

	// 6. Ждём "OK"
	conn.SetReadDeadline(time.Now().Add(15 * time.Second))
	msgType, ack, err := conn.ReadMessage()
	if err != nil {
		conn.Close()
		return fmt.Errorf("recv ack (msgType=%d dh=%x preshared=%q): %w", msgType, dhSecret[:4], t.cfg.Preshared, err)
	}
	if string(ack) != "OK" {
		conn.Close()
		return fmt.Errorf("handshake rejected ack=%q (dh=%x preshared=%q)", ack, dhSecret[:4], t.cfg.Preshared)
	}
	conn.SetReadDeadline(time.Time{})

	t.mu.Lock()
	t.conn = conn
	t.closed = false
	t.SharedKey = dhSecret
	t.mu.Unlock()

	go t.readLoop()
	go t.writeLoop()
	return nil
}

func (t *WSTransport) readLoop() {
	defer func() {
		t.mu.Lock()
		t.closed = true
		t.mu.Unlock()
		select {
		case <-t.done:
		default:
			close(t.done)
		}
		close(t.recvCh)
	}()
	for {
		t.conn.SetReadDeadline(time.Now().Add(90 * time.Second))
		_, data, err := t.conn.ReadMessage()
		if err != nil {
			return
		}
		atomic.AddInt64(&t.bytesRecv, int64(len(data)))
		select {
		case t.recvCh <- data:
		default: // переполнен — дропаем (padding chaff всё равно выбросим)
		}
	}
}

func (t *WSTransport) writeLoop() {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-t.done:
			return
		case data, ok := <-t.writeCh:
			if !ok {
				return
			}
			t.mu.Lock()
			if t.closed {
				t.mu.Unlock()
				return
			}
			t.conn.SetWriteDeadline(time.Now().Add(15 * time.Second))
			err := t.conn.WriteMessage(websocket.BinaryMessage, data)
			t.mu.Unlock()
			if err != nil {
				return
			}
		case <-ticker.C:
			t.mu.Lock()
			if !t.closed && t.conn != nil {
				t.conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
				t.conn.WriteMessage(websocket.PingMessage, nil)
			}
			t.mu.Unlock()
		}
	}
}

func (t *WSTransport) Send(data []byte) error {
	select {
	case t.writeCh <- data:
		atomic.AddInt64(&t.bytesSent, int64(len(data)))
		return nil
	case <-time.After(5 * time.Second):
		return fmt.Errorf("write timeout")
	}
}

func (t *WSTransport) Recv() ([]byte, bool) {
	data, ok := <-t.recvCh
	return data, ok
}

func (t *WSTransport) IsConnected() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return !t.closed && t.conn != nil
}

func (t *WSTransport) Close() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed || t.conn == nil {
		return
	}
	t.closed = true
	t.conn.Close()
}

// ─────────────────────────────────────────────────────────────────────────────
// ChannelManager (упрощённый: stripe + crypto + assembler)
// ─────────────────────────────────────────────────────────────────────────────

type streamBuf struct {
	chunks   map[uint64][]byte
	seen     map[uint64]bool
	nextSeq  uint64
	finSeq   uint64
	hasFin   bool
}

func newStreamBuf() *streamBuf {
	return &streamBuf{
		chunks:  make(map[uint64][]byte),
		seen:    make(map[uint64]bool),
		finSeq:  ^uint64(0),
	}
}

type ChannelManager struct {
	mu         sync.RWMutex
	transports []*WSTransport
	crypto     *CryptoSession
	rrCnt      uint64

	asmMu   sync.Mutex
	asmBufs map[uint32]*streamBuf

	sendSeqMu sync.Mutex
	sendSeqs  map[uint32]uint64

	onData   func(uint32, []byte)
	onFinish func(uint32)

	done chan struct{}
}

// deriveCrypto: совместимо с shared.py make_crypto(is_initiator=True)
//   combined = concat(sharedKey[ch0], sharedKey[ch1], ...)
//   c2s, s2c = HKDF(combined, preshared, "session_c2s", "session_s2c")
//   initiator: send=c2s, recv=s2c
func deriveCrypto(transports []*WSTransport, preshared []byte) (*CryptoSession, error) {
	combined := make([]byte, 0, 32*len(transports))
	for _, t := range transports {
		if len(t.SharedKey) != 32 {
			return nil, fmt.Errorf("transport %d: no shared key", t.id)
		}
		combined = append(combined, t.SharedKey...)
	}
	c2s, s2c, err := deriveKeys(combined, preshared, []byte("session_c2s"), []byte("session_s2c"))
	if err != nil {
		return nil, err
	}
	return NewCryptoSession(c2s, s2c), nil
}

func NewChannelManager(transports []*WSTransport, preshared []byte) (*ChannelManager, error) {
	crypto, err := deriveCrypto(transports, preshared)
	if err != nil {
		return nil, err
	}
	return &ChannelManager{
		transports: transports,
		crypto:     crypto,
		asmBufs:    make(map[uint32]*streamBuf),
		sendSeqs:   make(map[uint32]uint64),
		done:       make(chan struct{}),
	}, nil
}

func (cm *ChannelManager) SetCallbacks(onData func(uint32, []byte), onFinish func(uint32)) {
	cm.onData = onData
	cm.onFinish = onFinish
}

func (cm *ChannelManager) Start() {
	for _, t := range cm.transports {
		go cm.recvLoop(t)
	}
}

func (cm *ChannelManager) Stop() {
	select {
	case <-cm.done:
	default:
		close(cm.done)
	}
	cm.mu.RLock()
	for _, t := range cm.transports {
		t.Close()
	}
	cm.mu.RUnlock()
}

func (cm *ChannelManager) Stats() (sent, recv int64) {
	cm.mu.RLock()
	defer cm.mu.RUnlock()
	for _, t := range cm.transports {
		sent += atomic.LoadInt64(&t.bytesSent)
		recv += atomic.LoadInt64(&t.bytesRecv)
	}
	return
}

func (cm *ChannelManager) pick() *WSTransport {
	cm.mu.RLock()
	defer cm.mu.RUnlock()
	n := len(cm.transports)
	if n == 0 {
		return nil
	}
	for i := 0; i < n; i++ {
		idx := int(atomic.AddUint64(&cm.rrCnt, 1)-1) % n
		if cm.transports[idx].IsConnected() {
			return cm.transports[idx]
		}
	}
	return nil
}

// Send: data → Chunk → Encrypt → Pad → WS
func (cm *ChannelManager) Send(streamID uint32, data []byte, fin bool) error {
	const chunkSz = 1024

	var pieces [][]byte
	if len(data) == 0 {
		pieces = [][]byte{{}}
	} else {
		for len(data) > 0 {
			n := chunkSz
			if n > len(data) {
				n = len(data)
			}
			pieces = append(pieces, data[:n])
			data = data[n:]
		}
	}

	cm.sendSeqMu.Lock()
	baseSeq := cm.sendSeqs[streamID]
	cm.sendSeqs[streamID] = baseSeq + uint64(len(pieces))
	cm.sendSeqMu.Unlock()

	for i, piece := range pieces {
		t := cm.pick()
		if t == nil {
			return fmt.Errorf("no transport")
		}
		flags := flagData
		if fin && i == len(pieces)-1 {
			flags |= flagFIN
		}
		chunk := &Chunk{
			StreamID: streamID,
			Seq:      baseSeq + uint64(i),
			Flags:    flags,
			Channel:  byte(t.id),
			Payload:  piece,
		}
		raw := chunk.Pack()
		enc, err := cm.crypto.Encrypt(streamID, raw)
		if err != nil {
			return err
		}
		if err := t.Send(padData(enc)); err != nil {
			return err
		}
	}
	return nil
}

func (cm *ChannelManager) recvLoop(t *WSTransport) {
	for {
		select {
		case <-cm.done:
			return
		default:
		}
		packet, ok := t.Recv()
		if !ok {
			return
		}
		payload, err := unpadData(packet)
		if err != nil || payload == nil {
			continue
		}
		_, _, plaintext, err := cm.crypto.Decrypt(payload)
		if err != nil {
			continue
		}
		chunk, err := UnpackChunk(plaintext)
		if err != nil {
			continue
		}
		if chunk.Flags&flagPing != 0 {
			continue
		}
		cm.assemble(chunk)
	}
}

func (cm *ChannelManager) assemble(chunk *Chunk) {
	cm.asmMu.Lock()
	defer cm.asmMu.Unlock()

	sid := chunk.StreamID
	buf, ok := cm.asmBufs[sid]
	if !ok {
		buf = newStreamBuf()
		cm.asmBufs[sid] = buf
	}
	if buf.seen[chunk.Seq] {
		return
	}
	buf.seen[chunk.Seq] = true
	buf.chunks[chunk.Seq] = chunk.Payload
	if chunk.Flags&flagFIN != 0 {
		buf.hasFin = true
		buf.finSeq = chunk.Seq
	}

	for {
		data, found := buf.chunks[buf.nextSeq]
		if !found {
			break
		}
		delete(buf.chunks, buf.nextSeq)
		seq := buf.nextSeq
		buf.nextSeq++
		if len(data) > 0 && cm.onData != nil {
			cm.onData(sid, data)
		}
		if buf.hasFin && seq == buf.finSeq {
			if cm.onFinish != nil {
				cm.onFinish(sid)
			}
			delete(cm.asmBufs, sid)
			return
		}
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// SOCKS5 Server (только CONNECT; совместимый control header с socks5.py)
// ─────────────────────────────────────────────────────────────────────────────

const (
	socksVer    = byte(5)
	cmdCONNECT  = byte(1)
	socksIPv4   = byte(1)
	socksDomain = byte(3)
	socksIPv6   = byte(4)
	repOK       = byte(0)
	repFail     = byte(1)
)

type socksConn struct{ writer net.Conn }

type SOCKS5Server struct {
	cm       *ChannelManager
	port     int
	listener net.Listener

	connsMu sync.Mutex
	conns   map[uint32]*socksConn

	sidMu   sync.Mutex
	nextSID uint32
}

func NewSOCKS5Server(cm *ChannelManager, port int) *SOCKS5Server {
	s := &SOCKS5Server{
		cm:      cm,
		port:    port,
		conns:   make(map[uint32]*socksConn),
		nextSID: 100,
	}
	cm.SetCallbacks(s.onData, s.onFinish)
	return s
}

func (s *SOCKS5Server) alloc() uint32 {
	s.sidMu.Lock()
	defer s.sidMu.Unlock()
	id := s.nextSID
	s.nextSID++
	return id
}

func (s *SOCKS5Server) onData(sid uint32, data []byte) {
	s.connsMu.Lock()
	c, ok := s.conns[sid]
	s.connsMu.Unlock()
	if ok {
		c.writer.Write(data)
	}
}

func (s *SOCKS5Server) onFinish(sid uint32) {
	s.connsMu.Lock()
	c, ok := s.conns[sid]
	delete(s.conns, sid)
	s.connsMu.Unlock()
	if ok {
		c.writer.Close()
	}
}

func (s *SOCKS5Server) Start() error {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", s.port))
	if err != nil {
		return err
	}
	s.listener = ln
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go s.handle(conn)
		}
	}()
	return nil
}

func (s *SOCKS5Server) Stop() {
	if s.listener != nil {
		s.listener.Close()
	}
}

func (s *SOCKS5Server) handle(conn net.Conn) {
	defer func() {
		if r := recover(); r != nil {
			conn.Close()
		}
	}()
	conn.SetDeadline(time.Now().Add(30 * time.Second))

	// Greeting
	hdr := make([]byte, 2)
	if _, err := io.ReadFull(conn, hdr); err != nil || hdr[0] != socksVer {
		conn.Close()
		return
	}
	io.ReadFull(conn, make([]byte, hdr[1])) // methods
	conn.Write([]byte{socksVer, 0x00})       // NO AUTH

	// Request
	req := make([]byte, 4)
	if _, err := io.ReadFull(conn, req); err != nil || req[1] != cmdCONNECT {
		conn.Write([]byte{socksVer, 0x07, 0x00, socksIPv4, 0, 0, 0, 0, 0, 0})
		conn.Close()
		return
	}

	var addrRaw []byte
	atyp := req[3]
	switch atyp {
	case socksIPv4:
		addrRaw = make([]byte, 4)
		io.ReadFull(conn, addrRaw)
	case socksIPv6:
		addrRaw = make([]byte, 16)
		io.ReadFull(conn, addrRaw)
	case socksDomain:
		dlen := make([]byte, 1)
		io.ReadFull(conn, dlen)
		addrRaw = make([]byte, dlen[0])
		io.ReadFull(conn, addrRaw)
	default:
		conn.Write([]byte{socksVer, 0x08, 0x00, socksIPv4, 0, 0, 0, 0, 0, 0})
		conn.Close()
		return
	}

	portBuf := make([]byte, 2)
	io.ReadFull(conn, portBuf)
	port := binary.BigEndian.Uint16(portBuf)

	sid := s.alloc()
	s.connsMu.Lock()
	s.conns[sid] = &socksConn{writer: conn}
	s.connsMu.Unlock()

	// Control header (совместимо с socks5.py encode_control):
	// CONTROL_VERSION(1=0x01) || ATYP(1) || addr_bytes || port(2)
	ctrl := buildCtrl(atyp, addrRaw, port)
	if err := s.cm.Send(sid, ctrl, false); err != nil {
		conn.Write([]byte{socksVer, repFail, 0x00, socksIPv4, 0, 0, 0, 0, 0, 0})
		conn.Close()
		s.connsMu.Lock()
		delete(s.conns, sid)
		s.connsMu.Unlock()
		return
	}

	conn.Write([]byte{socksVer, repOK, 0x00, socksIPv4, 0, 0, 0, 0, 0, 0})
	conn.SetDeadline(time.Time{})

	// Читаем из браузера → туннель
	buf := make([]byte, 32*1024)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			s.cm.Send(sid, chunk, false)
		}
		if err != nil {
			s.cm.Send(sid, nil, true) // FIN
			break
		}
	}
	s.connsMu.Lock()
	delete(s.conns, sid)
	s.connsMu.Unlock()
}

// buildCtrl — encode_control из socks5.py
func buildCtrl(atyp byte, addr []byte, port uint16) []byte {
	const ctrlVer = byte(0x01)
	var addrField []byte
	if atyp == socksDomain {
		addrField = append([]byte{byte(len(addr))}, addr...)
	} else {
		addrField = addr
	}
	buf := make([]byte, 2+len(addrField)+2)
	buf[0] = ctrlVer
	buf[1] = atyp
	copy(buf[2:], addrField)
	binary.BigEndian.PutUint16(buf[2+len(addrField):], port)
	return buf
}

// ─────────────────────────────────────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────────────────────────────────────

type Config struct {
	ServerHost  string
	ServerPort  int  // базовый порт; канал i → ServerPort+i
	Socks5Port  int
	NumChannels int
	Preshared   []byte
	UseTLS      bool
	TLSInsecure bool
}

var defaultConfig = Config{
	ServerHost:  "147.90.14.155",
	ServerPort:  8443,
	Socks5Port:  1080,
	NumChannels: 3,
	Preshared:   []byte("CHANGE-ME-32-bytes-shared-secret"),
	UseTLS:      false,
	TLSInsecure: true,
}

// ─────────────────────────────────────────────────────────────────────────────
// ProxyApp
// ─────────────────────────────────────────────────────────────────────────────

type ProxyApp struct {
	cfg Config
	mu  sync.Mutex

	running    bool
	startTime  time.Time
	transports []*WSTransport
	chanMgr    *ChannelManager
	socks5     *SOCKS5Server

	statusDot    *StatusDot
	statusLabel  *widget.Label
	startBtn     *widget.Button
	stopBtn      *widget.Button
	channelCards []*ChannelCard
	uptime       *widget.Label
	bytesSent    *widget.Label
	bytesRecv    *widget.Label
	logBox       *fyne.Container
	logScroll    *container.Scroll
}

func (pa *ProxyApp) appendLog(msg string, col color.Color) {
	ts := time.Now().Format("15:04:05")
	pa.logBox.Add(logLine(ts, msg, col))
	pa.logScroll.ScrollToBottom()
	pa.logBox.Refresh()
}

func (pa *ProxyApp) setStatus(s string) {
	switch s {
	case "running":
		pa.statusDot.SetColor(colorGreen)
		pa.statusLabel.SetText("Running")
	case "connecting":
		pa.statusDot.SetColor(colorYellow)
		pa.statusLabel.SetText("Connecting…")
	case "stopped":
		pa.statusDot.SetColor(colorRed)
		pa.statusLabel.SetText("Stopped")
	default:
		pa.statusDot.SetColor(colorTextSub)
		pa.statusLabel.SetText("Idle")
	}
}

func (pa *ProxyApp) Start(host, portStr, numChStr, preshared, socksPortStr string, useTLS, tlsInsecure bool) {
	pa.mu.Lock()
	if pa.running {
		pa.mu.Unlock()
		return
	}
	pa.running = true
	pa.mu.Unlock()

	pa.cfg.ServerHost = host
	pa.cfg.Preshared = []byte(preshared)
	pa.cfg.UseTLS = useTLS
	pa.cfg.TLSInsecure = tlsInsecure
	fmt.Sscanf(portStr, "%d", &pa.cfg.ServerPort)
	fmt.Sscanf(socksPortStr, "%d", &pa.cfg.Socks5Port)
	fmt.Sscanf(numChStr, "%d", &pa.cfg.NumChannels)
	if pa.cfg.NumChannels < 1 {
		pa.cfg.NumChannels = 1
	}
	if pa.cfg.NumChannels > 8 {
		pa.cfg.NumChannels = 8
	}

	pa.startBtn.Disable()
	pa.stopBtn.Enable()
	pa.setStatus("connecting")
	pa.appendLog(fmt.Sprintf("Connecting %s:%d (%d ch)…", pa.cfg.ServerHost, pa.cfg.ServerPort, pa.cfg.NumChannels), colorAccent)
	for _, c := range pa.channelCards {
		c.SetStatus("idle")
	}

	go func() {
		var transports []*WSTransport
		for i := 0; i < pa.cfg.NumChannels; i++ {
			if i < len(pa.channelCards) {
				pa.channelCards[i].SetStatus("connecting")
			}
			t := NewWSTransport(i, pa.cfg)
			if err := t.Connect(); err != nil {
				pa.appendLog(fmt.Sprintf("Ch%d: %v", i+1, err), colorRed)
				if i < len(pa.channelCards) {
					pa.channelCards[i].SetStatus("error")
				}
				continue
			}
			pa.appendLog(fmt.Sprintf("Ch%d connected", i+1), colorGreen)
			if i < len(pa.channelCards) {
				pa.channelCards[i].SetStatus("connected")
			}
			transports = append(transports, t)
		}

		if len(transports) == 0 {
			pa.appendLog("No channels connected.", colorRed)
			pa.setStatus("stopped")
			pa.mu.Lock()
			pa.running = false
			pa.mu.Unlock()
			pa.startBtn.Enable()
			pa.stopBtn.Disable()
			return
		}

		cm, err := NewChannelManager(transports, pa.cfg.Preshared)
		if err != nil {
			pa.appendLog(fmt.Sprintf("Crypto: %v", err), colorRed)
			for _, t := range transports {
				t.Close()
			}
			pa.setStatus("stopped")
			pa.mu.Lock()
			pa.running = false
			pa.mu.Unlock()
			pa.startBtn.Enable()
			pa.stopBtn.Disable()
			return
		}

		socks5 := NewSOCKS5Server(cm, pa.cfg.Socks5Port)
		cm.Start()

		if err := socks5.Start(); err != nil {
			pa.appendLog(fmt.Sprintf("SOCKS5: %v", err), colorRed)
			cm.Stop()
			pa.setStatus("stopped")
			pa.mu.Lock()
			pa.running = false
			pa.mu.Unlock()
			pa.startBtn.Enable()
			pa.stopBtn.Disable()
			return
		}

		pa.mu.Lock()
		pa.transports = transports
		pa.chanMgr = cm
		pa.socks5 = socks5
		pa.startTime = time.Now()
		pa.mu.Unlock()

		pa.setStatus("running")
		pa.appendLog(fmt.Sprintf("SOCKS5 ready 127.0.0.1:%d", pa.cfg.Socks5Port), colorAccent)
		pa.appendLog(fmt.Sprintf("Channels: %d active", len(transports)), colorTextSub)

		go func() {
			tk := time.NewTicker(time.Second)
			defer tk.Stop()
			for range tk.C {
				pa.mu.Lock()
				running := pa.running
				st := pa.startTime
				cm := pa.chanMgr
				pa.mu.Unlock()
				if !running {
					return
				}
				el := time.Since(st)
				pa.uptime.SetText(fmt.Sprintf("%02d:%02d:%02d", int(el.Hours()), int(el.Minutes())%60, int(el.Seconds())%60))
				if cm != nil {
					s, r := cm.Stats()
					pa.bytesSent.SetText(humanBytes(s))
					pa.bytesRecv.SetText(humanBytes(r))
				}
			}
		}()
	}()
}

func (pa *ProxyApp) Stop() {
	pa.mu.Lock()
	defer pa.mu.Unlock()
	if pa.socks5 != nil {
		pa.socks5.Stop()
		pa.socks5 = nil
	}
	if pa.chanMgr != nil {
		pa.chanMgr.Stop()
		pa.chanMgr = nil
	}
	pa.transports = nil
	pa.running = false
	for _, c := range pa.channelCards {
		c.SetStatus("idle")
	}
	pa.setStatus("stopped")
	pa.appendLog("Stopped.", colorRed)
	pa.startBtn.Enable()
	pa.stopBtn.Disable()
	pa.uptime.SetText("00:00:00")
	pa.bytesSent.SetText("0 B")
	pa.bytesRecv.SetText("0 B")
}

// ─────────────────────────────────────────────────────────────────────────────
// UI
// ─────────────────────────────────────────────────────────────────────────────

func buildUI(a fyne.App) fyne.Window {
	w := a.NewWindow("OlegShifter")
	w.Resize(fyne.NewSize(420, 740))

	pa := &ProxyApp{cfg: defaultConfig, logBox: container.NewVBox()}

	// Header
	title := canvas.NewText("OlegShifter", colorTextMain)
	title.TextSize = 20
	title.TextStyle = fyne.TextStyle{Bold: true}
	sub := canvas.NewText("SOCKS5 over WebSocket", colorTextSub)
	sub.TextSize = 12
	pa.statusDot = NewStatusDot(colorTextSub)
	pa.statusLabel = widget.NewLabel("Idle")
	pa.statusLabel.Importance = widget.LowImportance
	header := container.NewBorder(nil, nil, nil,
		container.NewHBox(pa.statusDot, pa.statusLabel),
		container.NewVBox(title, sub))

	// Config
	mk := func(ph, val string) *widget.Entry {
		e := widget.NewEntry()
		e.SetPlaceHolder(ph)
		e.SetText(val)
		return e
	}
	hostE := mk("IP/hostname", defaultConfig.ServerHost)
	portE := mk("8443", fmt.Sprint(defaultConfig.ServerPort))
	chE := mk("1-8", fmt.Sprint(defaultConfig.NumChannels))
	socksE := mk("1080", fmt.Sprint(defaultConfig.Socks5Port))
	preE := widget.NewEntry()
	preE.SetPlaceHolder("preshared secret")
	preE.SetText(string(defaultConfig.Preshared))
	tlsCheck := widget.NewCheck("TLS (wss://)", nil)
	tlsInsCheck := widget.NewCheck("Skip TLS verify", nil)
	tlsInsCheck.SetChecked(defaultConfig.TLSInsecure)

	form := widget.NewForm(
		widget.NewFormItem("Host", hostE),
		widget.NewFormItem("Port", portE),
		widget.NewFormItem("Channels", chE),
		widget.NewFormItem("SOCKS5 Port", socksE),
		widget.NewFormItem("Preshared", preE),
		widget.NewFormItem("", tlsCheck),
		widget.NewFormItem("", tlsInsCheck),
	)

	// Channel cards
	const numCards = 4
	pa.channelCards = make([]*ChannelCard, numCards)
	cards := make([]fyne.CanvasObject, numCards)
	for i := 0; i < numCards; i++ {
		pa.channelCards[i] = NewChannelCard(i)
		cards[i] = pa.channelCards[i]
	}

	// Stats
	pa.uptime = widget.NewLabel("00:00:00")
	pa.uptime.TextStyle = fyne.TextStyle{Monospace: true}
	pa.bytesSent = widget.NewLabel("0 B")
	pa.bytesSent.TextStyle = fyne.TextStyle{Monospace: true}
	pa.bytesRecv = widget.NewLabel("0 B")
	pa.bytesRecv.TextStyle = fyne.TextStyle{Monospace: true}
	stats := container.NewGridWithColumns(3,
		container.NewVBox(secLabel("UPTIME"), pa.uptime),
		container.NewVBox(secLabel("↑ SENT"), pa.bytesSent),
		container.NewVBox(secLabel("↓ RECV"), pa.bytesRecv),
	)

	// Buttons
	pa.startBtn = widget.NewButton("▶  Start", func() {
		pa.Start(hostE.Text, portE.Text, chE.Text, preE.Text, socksE.Text, tlsCheck.Checked, tlsInsCheck.Checked)
	})
	pa.startBtn.Importance = widget.HighImportance
	pa.stopBtn = widget.NewButton("■  Stop", func() { pa.Stop() })
	pa.stopBtn.Importance = widget.DangerImportance
	pa.stopBtn.Disable()

	// Log
	pa.logScroll = container.NewScroll(pa.logBox)
	pa.logScroll.SetMinSize(fyne.NewSize(0, 180))
	clearBtn := widget.NewButton("Clear", func() { pa.logBox.RemoveAll(); pa.logBox.Refresh() })
	clearBtn.Importance = widget.LowImportance

	content := container.NewVBox(
		container.NewPadded(header), sep(),
		container.NewPadded(secLabel("CONFIGURATION")),
		container.NewPadded(form), sep(),
		container.NewPadded(secLabel("CHANNELS")),
		container.NewPadded(container.NewGridWithColumns(4, cards...)), sep(),
		container.NewPadded(stats), sep(),
		container.NewPadded(container.NewGridWithColumns(2, pa.startBtn, pa.stopBtn)), sep(),
		container.NewPadded(container.NewBorder(nil, nil, secLabel("LOG"), clearBtn)),
		container.NewPadded(pa.logScroll),
	)
	w.SetContent(container.NewVScroll(content))
	pa.appendLog("Ready. Configure and press Start.", colorTextSub)
	return w
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────

func main() {
	a := app.New()
	a.Settings().SetTheme(&darkTheme{})
	buildUI(a).ShowAndRun()
}
