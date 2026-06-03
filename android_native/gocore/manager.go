package olegcore

// ChannelManager — оркестратор multipath-передачи. Порт core/channel_manager.py
// + авто-реконнект из examples/client.py.

import (
	"fmt"
	"sync"
	"time"
)

const (
	pingIntervalSec = 15
	reconnectBase   = 1 * time.Second
	reconnectCap    = 60 * time.Second
)

type manager struct {
	mu         sync.Mutex
	transports map[int]*wsTransport
	configs    map[int]wsConfig
	crypto     *cryptoSession
	splitter   *splitter
	assembler  *assembler

	onData      func(streamID uint32, data []byte)
	onStreamEnd func(streamID uint32)

	running bool
	stopCh  chan struct{}
	wg      sync.WaitGroup
	logf    func(string)

	reconnecting map[int]bool
}

func newManager(crypto *cryptoSession, transports map[int]*wsTransport, configs map[int]wsConfig, logf func(string)) *manager {
	channels := make([]int, 0, len(transports))
	for id := range transports {
		channels = append(channels, id)
	}
	m := &manager{
		transports:   transports,
		configs:      configs,
		crypto:       crypto,
		splitter:     newSplitter(channels),
		stopCh:       make(chan struct{}),
		logf:         logf,
		reconnecting: make(map[int]bool),
	}
	m.assembler = newAssembler(
		func(sid uint32, data []byte) {
			if m.onData != nil {
				m.onData(sid, data)
			}
		},
		func(sid uint32) {
			if m.onStreamEnd != nil {
				m.onStreamEnd(sid)
			}
		},
	)
	return m
}

func (m *manager) log(format string, args ...interface{}) {
	if m.logf != nil {
		m.logf(fmt.Sprintf(format, args...))
	}
}

func (m *manager) start() {
	m.running = true
	m.mu.Lock()
	for chID, t := range m.transports {
		m.startRecvLoop(chID, t)
	}
	m.mu.Unlock()

	m.wg.Add(1)
	go m.pingLoop()
}

func (m *manager) stop() {
	m.running = false
	close(m.stopCh)
	m.mu.Lock()
	for _, t := range m.transports {
		t.close()
	}
	m.transports = make(map[int]*wsTransport)
	m.mu.Unlock()
	m.wg.Wait()
}

// send нарезает данные и отправляет по каналам.
func (m *manager) send(streamID uint32, data []byte, fin bool) error {
	if !m.running {
		return fmt.Errorf("manager не запущен")
	}
	plans := m.splitter.split(streamID, data, fin)
	for _, p := range plans {
		if err := m.sendChunkOnChannel(p.channel, p.chunk); err != nil {
			// канал упал — chunk потерян, но дедуп/реконнект разрулят
			m.log("канал %d упал при отправке: %v", p.channel, err)
		}
	}
	return nil
}

func (m *manager) sendChunkOnChannel(chID int, c *chunk) error {
	m.mu.Lock()
	t := m.transports[chID]
	m.mu.Unlock()
	if t == nil || !t.isConnected() {
		return fmt.Errorf("канал %d недоступен", chID)
	}

	raw := c.pack()
	enc, err := m.crypto.encrypt(chID, raw)
	if err != nil {
		return err
	}
	padded := pad(enc)
	if err := t.send(padded); err != nil {
		m.markChannelDown(chID, err.Error(), t)
		return err
	}
	return nil
}

func (m *manager) startRecvLoop(chID int, t *wsTransport) {
	m.wg.Add(1)
	go m.recvLoop(chID, t)
}

func (m *manager) recvLoop(chID int, t *wsTransport) {
	defer m.wg.Done()
	for m.running {
		packet, err := t.recv()
		if err != nil {
			if m.running {
				m.log("канал %d закрыт: %v", chID, err)
				m.markChannelDown(chID, err.Error(), t)
			}
			return
		}
		m.processIncoming(chID, packet)
	}
}

func (m *manager) processIncoming(chID int, packet []byte) {
	payload, err := unpad(packet)
	if err != nil {
		m.log("канал %d: ошибка padding: %v", chID, err)
		return
	}
	if payload == nil {
		return // chaff
	}
	_, _, plaintext, err := m.crypto.decrypt(payload)
	if err != nil {
		m.log("канал %d: ошибка расшифровки: %v", chID, err)
		return
	}
	c, err := unpackChunk(plaintext)
	if err != nil {
		m.log("канал %d: битый чанк: %v", chID, err)
		return
	}
	if c.isPing() {
		return // keepalive — соединение живо, ответ не нужен
	}
	m.assembler.receive(c)
}

func (m *manager) pingLoop() {
	defer m.wg.Done()
	ticker := time.NewTicker(pingIntervalSec * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-m.stopCh:
			return
		case <-ticker.C:
			m.mu.Lock()
			ids := make([]int, 0, len(m.transports))
			for id := range m.transports {
				ids = append(ids, id)
			}
			m.mu.Unlock()
			for _, chID := range ids {
				ping := &chunk{streamID: 0, seq: 0, flags: flagPing, channel: uint8(chID)}
				_ = m.sendChunkOnChannel(chID, ping)
			}
		}
	}
}

func (m *manager) markChannelDown(chID int, reason string, t *wsTransport) {
	m.mu.Lock()
	// если канал уже заменён другим транспортом — игнорируем
	if cur, ok := m.transports[chID]; ok && cur != t {
		m.mu.Unlock()
		return
	}
	delete(m.transports, chID)
	m.mu.Unlock()

	if t != nil {
		t.close()
	}
	m.splitter.removeChannel(chID)
	m.log("канал %d упал: %s — переподключаю...", chID, reason)
	m.triggerReconnect(chID)
}

func (m *manager) triggerReconnect(chID int) {
	m.mu.Lock()
	if m.reconnecting[chID] || !m.running {
		m.mu.Unlock()
		return
	}
	m.reconnecting[chID] = true
	m.mu.Unlock()

	m.wg.Add(1)
	go m.reconnectLoop(chID)
}

func (m *manager) reconnectLoop(chID int) {
	defer m.wg.Done()
	defer func() {
		m.mu.Lock()
		m.reconnecting[chID] = false
		m.mu.Unlock()
	}()

	cfg, ok := m.configs[chID]
	if !ok {
		return
	}
	delay := reconnectBase
	for m.running {
		select {
		case <-m.stopCh:
			return
		case <-time.After(delay):
		}
		t := newWSTransport(cfg)
		if err := t.connect(); err != nil {
			m.log("канал %d реконнект неудача (%v), жду %.0fs", chID, err, (delay * 2).Seconds())
			delay *= 2
			if delay > reconnectCap {
				delay = reconnectCap
			}
			continue
		}
		m.mu.Lock()
		m.transports[chID] = t
		m.mu.Unlock()
		m.splitter.addChannel(chID)
		m.startRecvLoop(chID, t)
		m.log("канал %d переподключён", chID)
		return
	}
}
