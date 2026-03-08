from astrbot.api.event import filter
from astrbot.api.star import Star, register


@register("generated_plugin", "AI", "generated plugin", "0.1.0")
class GeneratedPlugin(Star):
    @filter.command("test")
    async def test(self, event):

        yield event.plain_result("plugin works")
