package olegcore

// Порт core/padding.py (только то, что нужно клиенту: pad/unpad).
// Заголовок: type(1) + real_len(2 be). type: DATA=0x01, CHAFF=0x02, ACK=0x03.
// BIMODAL-стратегия как в client.py: два пика размеров для имитации HTTP.

import (
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"io"
	"math"
	"math/big"
)

const (
	paddingHeaderSize = 3
	pktData           = 0x01
	pktChaff          = 0x02
	pktAck            = 0x03

	padMinSize   = 64
	padMaxSize   = 1400
	padPeakSmall = 128
	padPeakLarge = 1200
	padPeakRatio = 0.3
)

// pad добавляет header(3) + data + случайный padding до target-размера.
func pad(data []byte) []byte {
	target := targetSize(len(data))
	padLen := target - paddingHeaderSize - len(data)
	if padLen < 0 {
		padLen = 0
	}

	out := make([]byte, paddingHeaderSize+len(data)+padLen)
	out[0] = pktData
	binary.BigEndian.PutUint16(out[1:3], uint16(len(data)))
	copy(out[paddingHeaderSize:], data)
	if padLen > 0 {
		_, _ = io.ReadFull(rand.Reader, out[paddingHeaderSize+len(data):])
	}
	return out
}

// unpad снимает padding. Возвращает (nil, nil) если пакет — chaff (выбросить).
func unpad(packet []byte) ([]byte, error) {
	if len(packet) < paddingHeaderSize {
		return nil, fmt.Errorf("пакет слишком короткий: %d", len(packet))
	}
	pktType := packet[0]
	realLen := int(binary.BigEndian.Uint16(packet[1:3]))

	if pktType == pktChaff {
		return nil, nil // мусор
	}
	if pktType != pktData && pktType != pktAck {
		return nil, fmt.Errorf("неизвестный тип пакета: %d", pktType)
	}

	end := paddingHeaderSize + realLen
	if end > len(packet) {
		return nil, fmt.Errorf("real_len больше размера пакета")
	}
	return packet[paddingHeaderSize:end], nil
}

func randFloat() float64 {
	n, _ := rand.Int(rand.Reader, big.NewInt(1<<53))
	return float64(n.Int64()) / float64(1<<53)
}

func gauss(mean, sigma float64) float64 {
	u1 := math.Max(randFloat(), 1e-10)
	u2 := randFloat()
	z := math.Sqrt(-2*math.Log(u1)) * math.Cos(2*math.Pi*u2)
	return mean + sigma*z
}

func targetSize(dataLen int) int {
	minimum := dataLen + paddingHeaderSize
	if padMinSize > minimum {
		minimum = padMinSize
	}

	var target float64
	if randFloat() < padPeakRatio {
		target = gauss(padPeakSmall, padPeakSmall*0.15)
	} else {
		target = gauss(padPeakLarge, padPeakLarge*0.08)
	}
	t := int(target)
	if t < minimum {
		t = minimum
	}
	if t > padMaxSize {
		t = padMaxSize
	}
	return t
}
