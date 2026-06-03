package main

import (
	"image/color"
	"log"
	"os"
	"time"

	"gioui.org/app"
	"gioui.org/font/gofont"
	"gioui.org/io/system"
	"gioui.org/layout"
	"gioui.org/op"
	"gioui.org/unit"
	"gioui.org/widget"
	"gioui.org/widget/material"
)

var ops op.Ops

func main() {
	// Запуск UI в отдельной горутине — требование для gioui mobile
	go func() {
		w := app.NewWindow(app.Title("Olegshifter Client"))
		if err := loop(w); err != nil {
			log.Fatal(err)
		}
		os.Exit(0)
	}()
	app.Main()
}

func loop(w *app.Window) error {
	th := material.NewTheme(gofont.Collection())
	var hostEditor, portEditor widget.Editor
	hostEditor.SingleLine = true
	portEditor.SingleLine = true
	hostEditor.SetText("127.0.0.1")
	portEditor.SetText("8080")
	var connectBtn widget.Clickable
	status := "disconnected"

	for e := range w.Events() {
		switch e := e.(type) {
		case system.DestroyEvent:
			return e.Err
		case system.FrameEvent:
			gtx := layout.NewContext(&ops, e)

			// Обработка нажатий
			for connectBtn.Clicked() {
				if status == "disconnected" {
					status = "connecting..."
					// имитация соединения — здесь должен быть реальный код подключения
					go func() {
						time.Sleep(1000 * time.Millisecond)
						status = "connected"
						// После реального соединения можно запускать дальнейшую логику
					}()
				} else {
					status = "disconnected"
				}
			}

			// Простая вёрстка: поля и кнопка
			layout.Center.Layout(gtx, func(gtx layout.Context) layout.Dimensions {
				return layout.Flex{Axis: layout.Vertical, Alignment: layout.Middle}.Layout(gtx,
					layout.Rigid(func(gtx layout.Context) layout.Dimensions {
						return material.H6(th, "Olegshifter").Layout(gtx)
					}),
					layout.Rigid(func(gtx layout.Context) layout.Dimensions {
						gtx.Constraints.Max.X = gtx.Px(unit.Dp(300))
						return material.Editor(th, &hostEditor, "Host").Layout(gtx)
					}),
					layout.Rigid(func(gtx layout.Context) layout.Dimensions {
						return layout.Inset{Top: unit.Dp(8)}.Layout(gtx, func(gtx layout.Context) layout.Dimensions {
							return material.Editor(th, &portEditor, "Port").Layout(gtx)
						})
					}),
					layout.Rigid(func(gtx layout.Context) layout.Dimensions {
						return layout.Inset{Top: unit.Dp(16)}.Layout(gtx, func(gtx layout.Context) layout.Dimensions {
							return material.Button(th, &connectBtn, "Connect").Layout(gtx)
						})
					}),
					layout.Rigid(func(gtx layout.Context) layout.Dimensions {
						return layout.Inset{Top: unit.Dp(12)}.Layout(gtx, func(gtx layout.Context) layout.Dimensions {
							lbl := material.Body1(th, "Status: "+status)
							lbl.Color = color.NRGBA{R: 0x22, G: 0x22, B: 0x22, A: 0xff}
							return lbl.Layout(gtx)
						})
					}),
				)
			})

			e.Frame(gtx.Ops)
		}
		}
	}
}
