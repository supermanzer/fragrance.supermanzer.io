from django.apps import AppConfig


class FragranceAppConfig(AppConfig):
    """
    Renamed from FragranceConfig to avoid collision with the FragranceConfig model.
    The ready() hook imports signals so the post_save receiver is registered at startup.
    """
    name = 'fragrance'

    def ready(self) -> None:
        import fragrance.signals  # noqa: F401 — side-effect import registers signal receivers
