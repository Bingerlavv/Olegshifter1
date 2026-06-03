package olegcore

// Порт core/multipath.py — формат чанка, splitter (нарезка по каналам),
// assembler (дедуп + восстановление порядка по seq).

import (
	"encoding/binary"
	"fmt"
	"sync"
)

const (
	chunkHeaderSize = 18   // magic(2)+stream(4)+seq(8)+flags(1)+length(2)+channel(1)
	maxChunkSize    = 1024 // как chunk_size в client.py (ChannelManagerConfig)
	dedupWindow     = 1024
)

var chunkMagic = []byte{0xAB, 0xCD}

const (
	flagData = 0x01
	flagFin  = 0x02
	flagPing = 0x04
)

type chunk struct {
	streamID uint32
	seq      uint64
	flags    uint8
	channel  uint8
	payload  []byte
}

func (c *chunk) isFin() bool  { return c.flags&flagFin != 0 }
func (c *chunk) isPing() bool { return c.flags&flagPing != 0 }

func (c *chunk) pack() []byte {
	out := make([]byte, chunkHeaderSize+len(c.payload))
	out[0] = chunkMagic[0]
	out[1] = chunkMagic[1]
	binary.BigEndian.PutUint32(out[2:6], c.streamID)
	binary.BigEndian.PutUint64(out[6:14], c.seq)
	out[14] = c.flags
	binary.BigEndian.PutUint16(out[15:17], uint16(len(c.payload)))
	out[17] = c.channel
	copy(out[18:], c.payload)
	return out
}

func unpackChunk(data []byte) (*chunk, error) {
	if len(data) < chunkHeaderSize {
		return nil, fmt.Errorf("слишком короткий чанк: %d", len(data))
	}
	if data[0] != chunkMagic[0] || data[1] != chunkMagic[1] {
		return nil, fmt.Errorf("неверный magic")
	}
	c := &chunk{
		streamID: binary.BigEndian.Uint32(data[2:6]),
		seq:      binary.BigEndian.Uint64(data[6:14]),
		flags:    data[14],
		channel:  data[17],
	}
	length := int(binary.BigEndian.Uint16(data[15:17]))
	if chunkHeaderSize+length > len(data) {
		return nil, fmt.Errorf("усечённый payload")
	}
	c.payload = append([]byte(nil), data[chunkHeaderSize:chunkHeaderSize+length]...)
	return c, nil
}

// splitter нарезает данные потока на чанки и раскладывает по каналам.
// Используем STRIPED (round-robin) — сервер всё равно дедуплицирует и
// упорядочивает по seq, так что любой выбор каналов совместим.
type splitter struct {
	mu       sync.Mutex
	channels []int
	seq      map[uint32]uint64 // seq на каждый stream_id
	rr       int
}

func newSplitter(channels []int) *splitter {
	cp := append([]int(nil), channels...)
	return &splitter{channels: cp, seq: make(map[uint32]uint64)}
}

type planItem struct {
	channel int
	chunk   *chunk
}

// split возвращает список (channel, chunk). Последний чанк помечается FIN если fin.
func (s *splitter) split(streamID uint32, data []byte, fin bool) []planItem {
	var pieces [][]byte
	if len(data) == 0 {
		pieces = [][]byte{{}}
	} else {
		for i := 0; i < len(data); i += maxChunkSize {
			end := i + maxChunkSize
			if end > len(data) {
				end = len(data)
			}
			pieces = append(pieces, data[i:end])
		}
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	if len(s.channels) == 0 {
		return nil
	}

	var result []planItem
	for idx, piece := range pieces {
		isLast := idx == len(pieces)-1
		flags := uint8(flagData)
		if isLast && fin {
			flags |= flagFin
		}
		seq := s.seq[streamID]
		s.seq[streamID] = seq + 1

		ch := s.channels[s.rr%len(s.channels)]
		s.rr++

		result = append(result, planItem{
			channel: ch,
			chunk: &chunk{
				streamID: streamID,
				seq:      seq,
				flags:    flags,
				channel:  uint8(ch),
				payload:  piece,
			},
		})
	}
	return result
}

func (s *splitter) addChannel(ch int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range s.channels {
		if c == ch {
			return
		}
	}
	s.channels = append(s.channels, ch)
}

func (s *splitter) removeChannel(ch int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.channels[:0]
	for _, c := range s.channels {
		if c != ch {
			out = append(out, c)
		}
	}
	s.channels = out
}

// streamBuffer — буфер незавершённого потока на приёме.
type streamBuffer struct {
	chunks           map[uint64][]byte
	finSeq           int64 // -1 = нет
	deliveredThrough int64 // последний доставленный seq, -1 в начале
}

func newStreamBuffer() *streamBuffer {
	return &streamBuffer{chunks: make(map[uint64][]byte), finSeq: -1, deliveredThrough: -1}
}

// assembler принимает чанки с любых каналов, дедуплицирует, восстанавливает
// порядок и вызывает onData/onStreamEnd.
type assembler struct {
	mu           sync.Mutex
	streams      map[uint32]*streamBuffer
	dedup        map[uint32]map[uint64]struct{}
	onData       func(streamID uint32, data []byte)
	onStreamEnd  func(streamID uint32)
}

func newAssembler(onData func(uint32, []byte), onStreamEnd func(uint32)) *assembler {
	return &assembler{
		streams:     make(map[uint32]*streamBuffer),
		dedup:       make(map[uint32]map[uint64]struct{}),
		onData:      onData,
		onStreamEnd: onStreamEnd,
	}
}

func (a *assembler) receive(c *chunk) {
	if c.isPing() {
		return
	}
	a.mu.Lock()

	sid := c.streamID
	seq := c.seq

	if a.dedup[sid] == nil {
		a.dedup[sid] = make(map[uint64]struct{})
	}
	if _, dup := a.dedup[sid][seq]; dup {
		a.mu.Unlock()
		return
	}
	a.dedup[sid][seq] = struct{}{}

	if len(a.dedup[sid]) > dedupWindow {
		// чистим минимальный seq
		var min uint64 = ^uint64(0)
		for s := range a.dedup[sid] {
			if s < min {
				min = s
			}
		}
		delete(a.dedup[sid], min)
	}

	buf := a.streams[sid]
	if buf == nil {
		buf = newStreamBuffer()
		a.streams[sid] = buf
	}
	buf.chunks[seq] = c.payload
	if c.isFin() {
		buf.finSeq = int64(seq)
	}

	// flush: доставляем подряд начиная с deliveredThrough+1.
	// Колбэки вызываем под локом, чтобы сохранить порядок записи в сокет, когда
	// чанки одного потока приходят с разных каналов (разных recv-горутин).
	// onData/onStreamEnd только пишут в сокет браузера и не вызывают assembler
	// повторно, поэтому дедлок невозможен.
	defer a.mu.Unlock()
	for {
		next := uint64(buf.deliveredThrough + 1)
		data, ok := buf.chunks[next]
		if !ok {
			break
		}
		delete(buf.chunks, next)
		buf.deliveredThrough = int64(next)
		if len(data) > 0 && a.onData != nil {
			a.onData(sid, data)
		}
		if buf.finSeq == int64(next) {
			delete(a.streams, sid)
			if a.onStreamEnd != nil {
				a.onStreamEnd(sid)
			}
			break
		}
	}
}
