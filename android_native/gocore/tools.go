//go:build tools

package olegcore

// Держим golang.org/x/mobile в графе зависимостей модуля — без этого
// `gomobile bind` падает с "missing golang.org/x/mobile dependency".
// Файл не компилируется (build-тег tools), он нужен только для go.mod/go mod tidy.
import (
	_ "golang.org/x/mobile/bind"
	_ "golang.org/x/mobile/cmd/gobind"
)
