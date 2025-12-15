from discord.ext import commands


class InstaSub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{__name__} is listening to the feed!")


async def setup(bot):
    await bot.add_cog(InstaSub(bot))
