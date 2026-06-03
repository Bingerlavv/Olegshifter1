//go:build !android && !linux

package olegcore

import "fmt"

// На десктопе (Windows/Mac) VPN-режим недоступен — есть только SOCKS5.
func startTunBridge(fd int, mtu int, socksAddr string, logf func(string)) (tunController, error) {
	return nil, fmt.Errorf("VPN-режим доступен только на Android")
}
