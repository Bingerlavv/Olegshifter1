package olegcore

// Криптоядро: X25519 + ChaCha20-Poly1305 + HKDF.
// Точный порт core/crypto_core.py и логики вывода ключей из examples/shared.py.
//
// Хендшейк по каждому каналу (transport.py):
//   1. клиент шлёт 32-байтный эфемерный X25519 pub
//   2. сервер отвечает своим 32-байтным pub
//   3. dh = X25519(priv, server_pub) — это shared_key канала
//   4. auth_key = HKDF(dh, salt=preshared, info="auth_c")
//   5. клиент шлёт HMAC-SHA256(auth_key, "handshake-ok")
//   6. сервер отвечает "OK"
//
// Сессионный ключ (один на все каналы, examples/shared.py):
//   combined = concat(shared_key каждого канала в порядке ch_id)
//   c2s = HKDF(combined, salt=preshared, info="session_c2s")
//   s2c = HKDF(combined, salt=preshared, info="session_s2c")

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"sync"
	"sync/atomic"

	"golang.org/x/crypto/chacha20poly1305"
	"golang.org/x/crypto/curve25519"
	"golang.org/x/crypto/hkdf"
)

const (
	nonceSize     = 12 // 96 бит для ChaCha20-Poly1305
	tagSize       = 16
	publicKeySize = 32
	headerSize    = 8 // stream_id(4) + seq(4)
)

// keyPair — эфемерная X25519 пара.
type keyPair struct {
	private [32]byte
	public  [32]byte
}

func generateKeyPair() (*keyPair, error) {
	var kp keyPair
	if _, err := io.ReadFull(rand.Reader, kp.private[:]); err != nil {
		return nil, err
	}
	// Стандартное X25519 base point умножение даёт публичный ключ.
	pub, err := curve25519.X25519(kp.private[:], curve25519.Basepoint)
	if err != nil {
		return nil, err
	}
	copy(kp.public[:], pub)
	return &kp, nil
}

// exchange — DH с публичным ключом партнёра.
func (kp *keyPair) exchange(theirPublic []byte) ([]byte, error) {
	return curve25519.X25519(kp.private[:], theirPublic)
}

// hkdf32 — HKDF-SHA256, 32 байта. Эквивалент _derive_keys одной info.
func hkdf32(secret, salt, info []byte) []byte {
	r := hkdf.New(sha256.New, secret, salt, info)
	out := make([]byte, 32)
	if _, err := io.ReadFull(r, out); err != nil {
		panic(err) // HKDF из памяти не падает
	}
	return out
}

// deriveSessionKeys собирает shared_key всех каналов (в порядке ch_id) и
// выводит c2s/s2c. sharedKeys должен быть уже отсортирован по ch_id.
func deriveSessionKeys(sharedKeys [][]byte, preshared []byte) (c2s, s2c []byte) {
	var combined []byte
	for _, k := range sharedKeys {
		combined = append(combined, k...)
	}
	c2s = hkdf32(combined, preshared, []byte("session_c2s"))
	s2c = hkdf32(combined, preshared, []byte("session_s2c"))
	return
}

// cryptoSession шифрует/дешифрует чанки. Один на все каналы, потокобезопасен.
// Заголовок (stream_id+seq) шифруется отдельно от тела — точно как в Python.
type cryptoSession struct {
	sendKey []byte
	recvKey []byte
	sendSeq uint64 // атомарный счётчик, общий по всем каналам
	mu      sync.Mutex
}

func newCryptoSession(sendKey, recvKey []byte) *cryptoSession {
	return &cryptoSession{sendKey: sendKey, recvKey: recvKey}
}

func makeNonce(seq uint64) []byte {
	// 4 нулевых байта + be64(seq) — как _make_nonce в Python.
	n := make([]byte, nonceSize)
	binary.BigEndian.PutUint64(n[4:], seq)
	return n
}

// encrypt: nonce(12) + header_ct(8+16) + body_ct(len+16).
func (cs *cryptoSession) encrypt(streamID int, plaintext []byte) ([]byte, error) {
	seq := atomic.AddUint64(&cs.sendSeq, 1) - 1 // первый seq = 0

	nonce := makeNonce(seq)

	header := make([]byte, headerSize)
	binary.BigEndian.PutUint32(header[0:4], uint32(streamID))
	binary.BigEndian.PutUint32(header[4:8], uint32(seq))

	aead, err := chacha20poly1305.New(cs.sendKey)
	if err != nil {
		return nil, err
	}

	headerCT := aead.Seal(nil, nonce, header, nonce)
	bodyCT := aead.Seal(nil, nonce, plaintext, headerCT)

	out := make([]byte, 0, len(nonce)+len(headerCT)+len(bodyCT))
	out = append(out, nonce...)
	out = append(out, headerCT...)
	out = append(out, bodyCT...)
	return out, nil
}

// decrypt возвращает (streamID, seq, plaintext).
func (cs *cryptoSession) decrypt(packet []byte) (int, uint32, []byte, error) {
	minSize := nonceSize + (headerSize + tagSize) + tagSize
	if len(packet) < minSize {
		return 0, 0, nil, fmt.Errorf("пакет слишком короткий: %d", len(packet))
	}

	nonce := packet[:nonceSize]
	headerCT := packet[nonceSize : nonceSize+headerSize+tagSize]
	bodyCT := packet[nonceSize+headerSize+tagSize:]

	aead, err := chacha20poly1305.New(cs.recvKey)
	if err != nil {
		return 0, 0, nil, err
	}

	header, err := aead.Open(nil, nonce, headerCT, nonce)
	if err != nil {
		return 0, 0, nil, fmt.Errorf("header auth fail: %w", err)
	}
	plaintext, err := aead.Open(nil, nonce, bodyCT, headerCT)
	if err != nil {
		return 0, 0, nil, fmt.Errorf("body auth fail: %w", err)
	}

	streamID := int(binary.BigEndian.Uint32(header[0:4]))
	seq := binary.BigEndian.Uint32(header[4:8])
	return streamID, seq, plaintext, nil
}
