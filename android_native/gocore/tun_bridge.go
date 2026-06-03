//go:build android || linux

package olegcore

// VPN-режим: терминируем IP-пакеты из TUN (Android VpnService) через
// gvisor-стек tun2socks и гоним TCP в наш локальный SOCKS5.
//
// Наш SOCKS5 умеет только TCP (CONNECT), поэтому:
//   - TCP        → SOCKS5 CONNECT к dst (по IP)
//   - UDP :53    → DNS-over-TCP через SOCKS5 к тому же DNS-серверу
//   - прочий UDP → отбрасываем (QUIC и т.п. откатываются на TCP)

import (
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"strconv"
	"sync"
	"time"

	"github.com/xjasonlyu/tun2socks/v2/core"
	"github.com/xjasonlyu/tun2socks/v2/core/adapter"
	"github.com/xjasonlyu/tun2socks/v2/core/device"
	"github.com/xjasonlyu/tun2socks/v2/core/device/fdbased"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

type tunBridge struct {
	socksAddr string
	logf      func(string)

	stack *stack.Stack
	dev   device.Device
}

// startTunBridge поднимает gvisor-стек на переданном fd TUN-устройства.
func startTunBridge(fd int, mtu int, socksAddr string, logf func(string)) (tunController, error) {
	if mtu <= 0 {
		mtu = 1500
	}
	dev, err := fdbased.Open(strconv.Itoa(fd), uint32(mtu), 0)
	if err != nil {
		return nil, fmt.Errorf("открыть TUN fd=%d: %w", fd, err)
	}

	b := &tunBridge{socksAddr: socksAddr, dev: dev, logf: logf}

	st, err := core.CreateStack(&core.Config{
		LinkEndpoint:     dev,
		TransportHandler: b,
	})
	if err != nil {
		dev.Close()
		return nil, fmt.Errorf("создать стек: %w", err)
	}
	b.stack = st
	logf(fmt.Sprintf("VPN активен (MTU %d) — весь трафик через туннель", mtu))
	return b, nil
}

func (b *tunBridge) stop() {
	if b.stack != nil {
		b.stack.Close()
		b.stack.Wait()
		b.stack = nil
	}
	if b.dev != nil {
		b.dev.Close()
		b.dev = nil
	}
}

// --- adapter.TransportHandler ---

func (b *tunBridge) HandleTCP(conn adapter.TCPConn) {
	defer conn.Close()
	id := conn.ID()
	dstIP := ipFromAddress(id.LocalAddress)
	if dstIP == nil {
		return
	}
	remote, err := b.dialViaSocks(dstIP, id.LocalPort)
	if err != nil {
		b.logf(fmt.Sprintf("[TCP] %s:%d: %v", dstIP, id.LocalPort, err))
		return
	}
	defer remote.Close()
	relay(conn, remote)
}

func (b *tunBridge) HandleUDP(conn adapter.UDPConn) {
	defer conn.Close()
	id := conn.ID()
	if id.LocalPort != 53 {
		return // не-DNS UDP не поддерживается TCP-only прокси
	}
	dstIP := ipFromAddress(id.LocalAddress)
	if dstIP == nil {
		return
	}
	b.handleDNS(conn, dstIP)
}

// handleDNS: читает UDP DNS-запросы и резолвит их DNS-over-TCP через SOCKS5.
func (b *tunBridge) handleDNS(conn adapter.UDPConn, dnsIP net.IP) {
	buf := make([]byte, 2048)
	for {
		conn.SetReadDeadline(time.Now().Add(30 * time.Second))
		n, err := conn.Read(buf)
		if err != nil {
			return
		}
		query := append([]byte(nil), buf[:n]...)
		resp, err := b.dnsOverTCP(dnsIP, query)
		if err != nil {
			b.logf(fmt.Sprintf("[DNS] %v", err))
			continue
		}
		if _, err := conn.Write(resp); err != nil {
			return
		}
	}
}

func (b *tunBridge) dnsOverTCP(dnsIP net.IP, query []byte) ([]byte, error) {
	sc, err := b.dialViaSocks(dnsIP, 53)
	if err != nil {
		return nil, err
	}
	defer sc.Close()

	sc.SetDeadline(time.Now().Add(10 * time.Second))

	var lenPrefix [2]byte
	binary.BigEndian.PutUint16(lenPrefix[:], uint16(len(query)))
	if _, err := sc.Write(lenPrefix[:]); err != nil {
		return nil, err
	}
	if _, err := sc.Write(query); err != nil {
		return nil, err
	}

	var respLen [2]byte
	if _, err := io.ReadFull(sc, respLen[:]); err != nil {
		return nil, err
	}
	resp := make([]byte, binary.BigEndian.Uint16(respLen[:]))
	if _, err := io.ReadFull(sc, resp); err != nil {
		return nil, err
	}
	return resp, nil
}

// dialViaSocks открывает соединение к нашему локальному SOCKS5 и делает CONNECT.
func (b *tunBridge) dialViaSocks(dstIP net.IP, dstPort uint16) (net.Conn, error) {
	c, err := net.DialTimeout("tcp", b.socksAddr, 5*time.Second)
	if err != nil {
		return nil, err
	}
	ok := false
	defer func() {
		if !ok {
			c.Close()
		}
	}()

	// greeting: NO AUTH
	if _, err := c.Write([]byte{0x05, 0x01, 0x00}); err != nil {
		return nil, err
	}
	resp := make([]byte, 2)
	if _, err := io.ReadFull(c, resp); err != nil {
		return nil, err
	}
	if resp[0] != 0x05 || resp[1] != 0x00 {
		return nil, fmt.Errorf("socks greeting отвергнут")
	}

	// CONNECT request
	req := []byte{0x05, 0x01, 0x00}
	if ip4 := dstIP.To4(); ip4 != nil {
		req = append(req, 0x01)
		req = append(req, ip4...)
	} else {
		req = append(req, 0x04)
		req = append(req, dstIP.To16()...)
	}
	var p [2]byte
	binary.BigEndian.PutUint16(p[:], dstPort)
	req = append(req, p[:]...)
	if _, err := c.Write(req); err != nil {
		return nil, err
	}

	// reply: ver rep rsv atyp + bnd_addr + bnd_port
	head := make([]byte, 4)
	if _, err := io.ReadFull(c, head); err != nil {
		return nil, err
	}
	if head[1] != 0x00 {
		return nil, fmt.Errorf("socks rep=%d", head[1])
	}
	var skip int
	switch head[3] {
	case 0x01:
		skip = 4
	case 0x04:
		skip = 16
	case 0x03:
		l := make([]byte, 1)
		if _, err := io.ReadFull(c, l); err != nil {
			return nil, err
		}
		skip = int(l[0])
	default:
		return nil, fmt.Errorf("socks atyp=%d", head[3])
	}
	if _, err := io.CopyN(io.Discard, c, int64(skip+2)); err != nil {
		return nil, err
	}

	ok = true
	return c, nil
}

// --- helpers ---

func ipFromAddress(a tcpip.Address) net.IP {
	switch a.Len() {
	case 4:
		b := a.As4()
		return net.IP(b[:])
	case 16:
		b := a.As16()
		return net.IP(b[:])
	}
	return nil
}

// relay двунаправленно перекачивает данные между двумя net.Conn.
func relay(a, b net.Conn) {
	var wg sync.WaitGroup
	wg.Add(2)
	cp := func(dst, src net.Conn) {
		defer wg.Done()
		io.Copy(dst, src)
		if cw, ok := dst.(interface{ CloseWrite() error }); ok {
			cw.CloseWrite()
		}
		dst.SetReadDeadline(time.Now().Add(5 * time.Second))
	}
	go cp(a, b)
	go cp(b, a)
	wg.Wait()
}
