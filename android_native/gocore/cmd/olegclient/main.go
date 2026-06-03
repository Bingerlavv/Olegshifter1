// Десктоп-обёртка для проверки Go-ядра против Python-сервера.
//
//	go run ./cmd/olegclient -host 127.0.0.1 -base 8443 -channels 3 -secret "CHANGE-ME-32-bytes-shared-secret" -tls -insecure
//
// Затем: curl --socks5 127.0.0.1:1080 http://example.com
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"olegcore"
)

type stdoutLogger struct{}

func (stdoutLogger) Log(line string) { log.Println(line) }

func main() {
	host := flag.String("host", "127.0.0.1", "адрес сервера")
	base := flag.Int("base", 8443, "первый WS-порт")
	channels := flag.Int("channels", 3, "сколько WS-каналов")
	socksHost := flag.String("socks-host", "127.0.0.1", "хост SOCKS5")
	socksPort := flag.Int("socks-port", 1080, "порт SOCKS5")
	secret := flag.String("secret", "CHANGE-ME-32-bytes-shared-secret", "preshared секрет")
	useTLS := flag.Bool("tls", false, "использовать wss://")
	insecure := flag.Bool("insecure", false, "не проверять TLS-сертификат")
	sni := flag.String("sni", "", "SNI/ServerName для TLS")
	flag.Parse()

	c := olegcore.NewClient()
	if err := c.Start(*host, *base, *channels, *socksHost, *socksPort, *secret, *useTLS, *insecure, *sni, stdoutLogger{}); err != nil {
		fmt.Fprintln(os.Stderr, "ОШИБКА:", err)
		os.Exit(1)
	}
	defer c.Stop()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	log.Println("Останавливаю клиент...")
}
