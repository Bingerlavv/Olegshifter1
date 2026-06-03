package olegcore

// SOCKS5-сервер клиентской стороны. Порт SOCKS5Server из core/socks5.py.
// Слушает локально, на каждое CONNECT выделяет stream_id и туннелит через manager.

import (
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"sync"
)

const (
	socksVersion = 0x05
	authNone     = 0x00
	cmdConnect   = 0x01
	atypIPv4     = 0x01
	atypDomain   = 0x03
	atypIPv6     = 0x04
	repSuccess   = 0x00
	repCmdNotSup = 0x07
	repAtypNotSup = 0x08

	controlVersion   = 0x01
	firstStreamID    = 100 // 0-99 зарезервированы под служебку
)

// encodeControl формирует control header: version(1) atyp(1) addr port(2 be).
// Для домена addr предваряется байтом длины.
func encodeControl(atyp byte, addr []byte, port int) []byte {
	var addrBytes []byte
	if atyp == atypDomain {
		addrBytes = append([]byte{byte(len(addr))}, addr...)
	} else {
		addrBytes = addr
	}
	out := make([]byte, 0, 2+len(addrBytes)+2)
	out = append(out, controlVersion, atyp)
	out = append(out, addrBytes...)
	p := make([]byte, 2)
	binary.BigEndian.PutUint16(p, uint16(port))
	out = append(out, p...)
	return out
}

type socks5Server struct {
	mgr      *manager
	listener net.Listener
	host     string
	port     int

	mu       sync.Mutex
	streams  map[uint32]net.Conn
	nextID   uint32
}

func newSocks5Server(mgr *manager, host string, port int) *socks5Server {
	s := &socks5Server{
		mgr:     mgr,
		host:    host,
		port:    port,
		streams: make(map[uint32]net.Conn),
		nextID:  firstStreamID,
	}
	mgr.onData = s.onTunnelData
	mgr.onStreamEnd = s.onTunnelEnd
	return s
}

func (s *socks5Server) start() error {
	ln, err := net.Listen("tcp", fmt.Sprintf("%s:%d", s.host, s.port))
	if err != nil {
		return err
	}
	s.listener = ln
	go s.acceptLoop()
	return nil
}

func (s *socks5Server) stop() {
	if s.listener != nil {
		s.listener.Close()
	}
	s.mu.Lock()
	for _, c := range s.streams {
		c.Close()
	}
	s.streams = make(map[uint32]net.Conn)
	s.mu.Unlock()
}

func (s *socks5Server) acceptLoop() {
	for {
		conn, err := s.listener.Accept()
		if err != nil {
			return // listener закрыт
		}
		go s.handleConn(conn)
	}
}

func (s *socks5Server) handleConn(conn net.Conn) {
	defer conn.Close()
	defer recoverLogf(s.mgr.logf, "socks handleConn")

	// 1. Greeting: [ver][nmethods][methods...]
	head := make([]byte, 2)
	if _, err := io.ReadFull(conn, head); err != nil {
		return
	}
	if head[0] != socksVersion {
		return
	}
	nmethods := int(head[1])
	if nmethods > 0 {
		if _, err := io.ReadFull(conn, make([]byte, nmethods)); err != nil {
			return
		}
	}
	// Отвечаем NO AUTH
	if _, err := conn.Write([]byte{socksVersion, authNone}); err != nil {
		return
	}

	// 2. Request: [ver][cmd][rsv][atyp][addr][port]
	reqHead := make([]byte, 4)
	if _, err := io.ReadFull(conn, reqHead); err != nil {
		return
	}
	cmd, atyp := reqHead[1], reqHead[3]
	if cmd != cmdConnect {
		s.sendReply(conn, repCmdNotSup)
		return
	}

	var addrRaw []byte
	switch atyp {
	case atypIPv4:
		addrRaw = make([]byte, 4)
		if _, err := io.ReadFull(conn, addrRaw); err != nil {
			return
		}
	case atypIPv6:
		addrRaw = make([]byte, 16)
		if _, err := io.ReadFull(conn, addrRaw); err != nil {
			return
		}
	case atypDomain:
		lb := make([]byte, 1)
		if _, err := io.ReadFull(conn, lb); err != nil {
			return
		}
		addrRaw = make([]byte, int(lb[0]))
		if _, err := io.ReadFull(conn, addrRaw); err != nil {
			return
		}
	default:
		s.sendReply(conn, repAtypNotSup)
		return
	}

	portBuf := make([]byte, 2)
	if _, err := io.ReadFull(conn, portBuf); err != nil {
		return
	}
	port := int(binary.BigEndian.Uint16(portBuf))

	// 3. Выделяем stream и шлём control header на exit node
	streamID := s.allocStream(conn)
	control := encodeControl(atyp, addrRaw, port)
	if err := s.mgr.send(streamID, control, false); err != nil {
		s.sendReply(conn, repCmdNotSup)
		s.removeStream(streamID)
		return
	}

	// 4. Сообщаем браузеру что соединение готово
	if err := s.sendReply(conn, repSuccess); err != nil {
		s.removeStream(streamID)
		return
	}

	// 5. Пайпим браузер → туннель
	s.pipeToTunnel(conn, streamID)
}

func (s *socks5Server) pipeToTunnel(conn net.Conn, streamID uint32) {
	buf := make([]byte, 4096)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			if serr := s.mgr.send(streamID, buf[:n], false); serr != nil {
				break
			}
		}
		if err != nil {
			s.mgr.send(streamID, nil, true) // FIN
			break
		}
	}
	s.removeStream(streamID)
}

// onTunnelData — данные из туннеля пишем в браузер.
func (s *socks5Server) onTunnelData(streamID uint32, data []byte) {
	s.mu.Lock()
	conn := s.streams[streamID]
	s.mu.Unlock()
	if conn == nil {
		return
	}
	_, _ = conn.Write(data)
}

func (s *socks5Server) onTunnelEnd(streamID uint32) {
	s.mu.Lock()
	conn := s.streams[streamID]
	delete(s.streams, streamID)
	s.mu.Unlock()
	if conn != nil {
		conn.Close()
	}
}

func (s *socks5Server) allocStream(conn net.Conn) uint32 {
	s.mu.Lock()
	defer s.mu.Unlock()
	id := s.nextID
	s.nextID++
	s.streams[id] = conn
	return id
}

func (s *socks5Server) removeStream(streamID uint32) {
	s.mu.Lock()
	delete(s.streams, streamID)
	s.mu.Unlock()
}

// sendReply отвечает SOCKS5-клиенту с нулевым bind-адресом.
func (s *socks5Server) sendReply(conn net.Conn, rep byte) error {
	reply := []byte{socksVersion, rep, 0x00, atypIPv4, 0, 0, 0, 0, 0, 0}
	_, err := conn.Write(reply)
	return err
}
