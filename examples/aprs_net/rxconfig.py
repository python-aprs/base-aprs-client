import reflex as rx

config = rx.Config(
    app_name="aprs_net",
    plugins=[
        rx.plugins.RadixThemesPlugin(theme=rx.theme(accent_color="indigo")),
        rx.plugins.SitemapPlugin(),
    ],
)
