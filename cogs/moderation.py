import datetime

import discord
from discord import app_commands
from discord.ext import commands


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{__name__} is ready to mod!")

    @app_commands.command(name="kick", description="Kicks a specified member.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
        forward: bool | None,
    ):
        if forward:
            await member.send(reason)
        await interaction.guild.kick(member)
        await interaction.response.send_message(
            f'{member.mention} has walked the plank! The reason why is apparently "{reason}". Ouch. Courtesy of {interaction.user.mention}!',
            ephemeral=True,
        )

    @app_commands.command(
        name="ban", description="Bans a specified member from the server."
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ):
        await interaction.guild.ban(member)
        await interaction.response.send_message(
            f'{member.mention} has kicked the bucket! The reason why is apparently "{reason}". Damn. Kinda harsh don\'t you think? Thanks, {interaction.user.mention}.',
            ephemeral=True,
        )

    @app_commands.command(
        name="unban", description="Unbans a specified member by their user ID."
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, member_identifier: str):
        await interaction.response.defer(ephemeral=True)
        try:
            member_name = member_identifier

            async for ban_entry in interaction.guild.bans():
                user = ban_entry.user
                if (user.name) == (member_name):
                    await interaction.guild.unban(user)
                    await interaction.followup.send(
                        f"Unbanned {user.name}. Welcome back!"
                    )
                    return

            await interaction.followup.send(
                f"Okay, um... says here '{member_identifier}' isn't, like, banned? Try a different user.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have the necessary permissions to unban users."
            )
        except Exception as e:
            print(e)

    @app_commands.command(name="mute", description="Timeout a user for a set duration.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: int,
        *,
        reason: str,
    ):
        await interaction.response.defer(ephemeral=True)
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        await interaction.followup.send(
            f"Timed out {member.mention} for {minutes} minutes! Were they really that annoying..?",
            ephemeral=True,
        )

    @app_commands.command(name="unmute", description="Removes timeout from a user.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unmute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        *,
        reason: str | None,
    ):
        await interaction.response.defer()
        if member.timed_out_until:
            await member.edit(timed_out_until=None, reason=reason)
            await interaction.followup.send(
                f"{member.mention} has been brought back from the depths! Welcome back!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"{member.mention} isn\t muted. I can't bring someone back if they aren't gone!",
                ephemeral=True,
            )


async def setup(bot):
    await bot.add_cog(Moderation(bot))
