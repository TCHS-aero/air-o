import discord
from discord import app_commands
from discord.ext import commands


class Testing_Commands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{__name__} has checked in!")

    @app_commands.command(
        name="ping",
        description="Latency in milliseconds. In basic terms, how laggy the bot is.",
    )
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ping_embed = discord.Embed(
            title="Ping",
            description="Latency in milliseconds. In basic terms, how laggy the bot is.",
            color=discord.Color.blue(),
        )
        ping_embed.add_field(
            name=f"{self.bot.user.name}'s Latency (ms):",
            value=f"{round(self.bot.latency * 1000)} ms",
            inline=False,
        )
        ping_embed.set_author(
            name=f"Requested by {interaction.user.name}",
            icon_url=interaction.user.display_avatar.url,
        )
        await interaction.followup.send(embed=ping_embed)


async def setup(bot):
    await bot.add_cog(Testing_Commands(bot))
