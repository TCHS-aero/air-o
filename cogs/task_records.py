import re
import sqlite3
from datetime import datetime, time, timedelta
from time import ctime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from db import (
    DB_PATH,
    complete_task,
    create_task,
    get_checkin_channel,
    get_task_by_id,
    get_task_id_by_name,
    init_db,
    set_checkin_channel,
)

CAPTAIN_ROLE_NAME = "Team Captain"


class TaskManagement(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id FROM tasks WHERE active = 1")
            rows = cur.fetchall()
            for (task_id,) in rows:
                try:
                    self.bot.add_view(CheckinView(task_id=task_id))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self.checkin_loop.start()

    def cog_unload(self):
        self.checkin_loop.cancel()

    def within_waking_hours(self):
        """
        Checks to see if the current time is within waking hours.

        - Waking hours are between 9:00AM and 9:00PM (21:00)
        """
        if time(9, 9, 9) <= datetime.now().time() <= time(21, 0, 0):
            return True
        return False

    async def interaction_is_captain(self, interaction: discord.Interaction) -> bool:
        """
        Checks to see if the current time is within waking hours.

        - Waking hours are between 9:00AM and 9:00PM (21:00)
        """
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return False
        return any(r.name == CAPTAIN_ROLE_NAME for r in interaction.user.roles)

    @app_commands.command(
        name="set_checkin_channel",
        description="Setup a channel for checkins to be forwarded to.",
    )
    @app_commands.describe(
        channel_id="Either the numerical value, or #<channel name>",
    )
    async def set_checkin_channel(
        self, interaction: discord.Interaction, channel_id: str
    ):
        """
        Sets the channel for user check-ins to be routed to.

        Behavior:
        - Checks to see if user is a Team Captain
        - Converts channel reference to id if need be
        - uses get_checkin_channel() to verify if a channel already is set
        - if not the same channel as is already set, then changes the channel id to match
        """
        if not await self.interaction_is_captain(interaction):
            await interaction.response.send_message(
                "Only team captains can set a checkin channel tasks.",
                ephemeral=True,
            )
            return

        try:
            no_whitespace: str = channel_id.strip()
            if no_whitespace.startswith("<#"):
                channel_id = int(no_whitespace[2:-1])
            else:
                channel_id = int(no_whitespace)
        except Exception as e:
            await interaction.response.send_message(
                f"Please use a valid channel id...\n{e}",
                ephemeral=True,
            )
            return

        try:
            checkin_channel_id = get_checkin_channel(interaction.guild_id)
        except Exception:
            checkin_channel_id = None

        if checkin_channel_id == channel_id:
            await interaction.response.send_message(
                "This channel has already been set as the checkin channel!",
                ephemeral=True,
            )
            return

        try:
            set_checkin_channel(interaction.guild_id, channel_id)
        except Exception as e:
            await interaction.response.send_message(
                f"Error setting channel: {e}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Channel set successfully!",
            ephemeral=True,
        )

    @app_commands.command(
        name="assign_task", description="Create a task thread and assign users."
    )
    @app_commands.describe(
        name="Short name of the task",
        assignees="Users to assign to this task",
        reminder_duration="The amount of time, in hours, to send a reminder to the assignees in the thread to check-in.",
    )
    async def assign_task(
        self,
        interaction: discord.Interaction,
        name: str,
        assignees: str,
        reminder_duration: Optional[int] = 16,
    ):
        """
        Creates a new assignment for a set of given users.

        Behavior:
        - Creates a new thread with a welcome message in the channel that the command was called in
        - Pings all assignees and pins check-in message
        - Saves check-in button to a new view for persistence between reboots
        """

        if not await self.interaction_is_captain(interaction):
            await interaction.response.send_message(
                "Only team captains can assign tasks.",
                ephemeral=True,
            )
            return

        try:
            checkin_channel_id = get_checkin_channel(interaction.guild_id)
        except Exception:
            checkin_channel_id = None

        if checkin_channel_id is None:
            await interaction.response.send_message(
                "A check-in channel has not been configured yet. Please set one before creating tasks (use the setup command).",
                ephemeral=True,
            )
            return

        ids = set(int(x) for x in re.findall(r"\d{15,20}", assignees))
        guild = interaction.guild
        assignee_members = []
        if guild is not None:
            for i in ids:
                member = guild.get_member(i)
                if member is None:
                    try:
                        member = await guild.fetch_member(i)
                    except Exception:
                        member = None
                if member:
                    assignee_members.append(member)
        assignees = assignee_members

        if interaction.channel is None or not isinstance(
            interaction.channel, discord.TextChannel
        ):
            await interaction.response.send_message(
                "This command must be used in a text channel, sorry!",
                ephemeral=True,
            )
            return

        if interaction.guild_id is not None and get_task_id_by_name(
            interaction.guild_id, name
        ):
            await interaction.response.send_message(
                f"A task named `{name}` already exists in this guild. Choose a different name, please!",
                ephemeral=True,
            )
            return

        thread = await interaction.channel.create_thread(
            name=f"Task: {name}",
            type=discord.ChannelType.public_thread,
        )

        task_id = create_task(
            guild_id=interaction.guild_id,
            thread_id=thread.id,
            captain_id=interaction.user.id,
            name=name,
            assignee_ids=[m.id for m in assignees],
            due_interval_hours=reminder_duration,
        )

        if task_id is None:
            try:
                await thread.delete()
            except Exception:
                pass
            await interaction.response.send_message(
                f"Could not create task `{name}` because a task with that name already exists. Let's be unique, hm?",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Task: {name}",
            description="Use the Check-in button to report your progress for today!",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Assignees",
            value=", ".join(m.mention for m in assignees)
            if assignees
            else "No assignees",
            inline=False,
        )
        embed.set_footer(
            text=f"Daily check-ins required within {reminder_duration} hours of the last response."
        )

        view = CheckinView(task_id=task_id)

        try:
            first_msg = await thread.send(embed=embed, view=view)
            try:
                await first_msg.pin()
            except Exception:
                pass
        except Exception:
            first_msg = None

        try:
            self.bot.add_view(view)
        except Exception:
            pass

        await interaction.response.send_message(
            f"Task `{name}` created in {thread.mention} and assigned to the given assignees! Assignees can only be registered on creation. However, anybody can send a check-in!",
            ephemeral=True,
        )

    @app_commands.command(
        name="cleanup_task", description="Mark a task complete and archive its thread."
    )
    @app_commands.describe(
        task_name="Name of the task to complete and clean up.",
        archive_thread="Whether to archive the task thread.",
    )
    async def cleanup_task(
        self,
        interaction: discord.Interaction,
        task_name: str,
        archive_thread: Optional[bool] = True,
    ):
        """
        Closes an active task.

        Behavior:
        - Locks and archives the thread associated with a task
        - Removes relevant information from the database
        """

        if not await self.interaction_is_captain(interaction):
            await interaction.response.send_message(
                "Only team captains can complete tasks.",
                ephemeral=True,
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "You can only cleanup guild tasks in a guild, silly!",
                ephemeral=True,
            )
            return

        task_id = get_task_id_by_name(interaction.guild_id, task_name)
        if task_id is None:
            await interaction.response.send_message(
                f'The task "{task_name}" doesn\'t exist here... are you sure you spelled it right?',
                ephemeral=True,
            )
            return

        task = get_task_by_id(task_id)
        thread_id = task["thread_id"] if task else None

        deleted = complete_task(interaction.guild_id, task_name)
        if not deleted:
            await interaction.response.send_message(
                f'Failed to remove task "{task_name}".',
                ephemeral=True,
            )
            return

        if archive_thread and thread_id:
            thread_channel: Optional[discord.Thread] = None
            thread_channel = self.bot.get_channel(thread_id)
            if thread_channel is None:
                try:
                    thread_channel = await self.bot.fetch_channel(thread_id)
                except Exception:
                    thread_channel = None

            if thread_channel is not None:
                try:
                    await thread_channel.edit(archived=True, locked=True)
                except Exception:
                    pass

        await interaction.response.send_message(
            f'Task "{task_name}" marked complete. Assignees will no longer be prompted for check-ins.',
            ephemeral=True,
        )

    @app_commands.command(
        name="list_tasks",
        description="List all currently open (active) tasks in this guild.",
    )
    async def list_tasks(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command must be run in a server (guild) channel, not in DMs.",
                ephemeral=True,
            )
            return

        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, thread_id, captain_id, due_interval_hours FROM tasks WHERE guild_id = ? AND active = 1 ORDER BY id",
                (guild_id,),
            )
            rows = cur.fetchall()
        except Exception as e:
            await interaction.response.send_message(
                f"Failed to read tasks from the database: {e}", ephemeral=True
            )
            try:
                conn.close()
            except Exception:
                pass
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not rows:
            await interaction.response.send_message(
                "There are no open tasks in this guild.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Open Tasks",
            description=f"Currently {len(rows)} open task(s) in this server.",
            color=discord.Color.blurple(),
        )

        for task_id, name, thread_id, captain_id, due_interval in rows:
            thread_mention = f"<#{thread_id}>"
            try:
                ch = self.bot.get_channel(thread_id)
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(thread_id)
                    except Exception:
                        ch = None
                if ch is not None and hasattr(ch, "mention"):
                    thread_mention = ch.mention
            except Exception:
                pass

            assignees_list = []
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute(
                    "SELECT user_id FROM task_assignees WHERE task_id = ?", (task_id,)
                )
                ass_rows = cur.fetchall()
                assignees_list = [f"<@{r[0]}>" for r in ass_rows] if ass_rows else []
            except Exception:
                assignees_list = []
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            assignees_text = (
                ", ".join(assignees_list) if assignees_list else "No assignees"
            )
            captain_mention = f"<@{captain_id}>"

            field_name = f"Name: {name}"
            field_value = (
                f"Thread: {thread_mention}\n"
                f"Captain: {captain_mention}\n"
                f"Assignees: {assignees_text}\n"
                f"Reminder (hours): {due_interval}"
            )

            embed.add_field(name=field_name, value=field_value, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(seconds=10)
    async def checkin_loop(self):
        """
        Sends check-in reminders to all assignees.
        """

        print("looking for opened tasks")
        current_time = datetime.utcnow()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT thread_id, next_check_time, due_interval_hours FROM tasks WHERE next_check_time <= ?",
            (current_time,),
        )
        due_threads = cursor.fetchall()

        for thread_id, next_check, due_interval_hours in due_threads:
            try:
                if not self.within_waking_hours():
                    print("not within waking hours")
                    continue

                thread = await self.bot.fetch_channel(thread_id)

                await thread.send(
                    "Don't forget to send in your check-ins if you haven't already!"
                )

                next_check_time = current_time + timedelta(seconds=due_interval_hours)
                with sqlite3.connect(DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE tasks SET next_check_time = ? WHERE thread_id = ?",
                        (next_check_time, thread_id),
                    )
                    conn.commit()

            except Exception as e:
                print(f"Failed to send message to thread {thread_id}: {e}")

    @checkin_loop.before_loop
    async def before_checkin_loop(self):
        await self.bot.wait_until_ready()


class CheckinSelect(discord.ui.Select):
    def __init__(self, task_id: int):
        options = [
            discord.SelectOption(
                label="Done!",
                value="done",
                description="Everything assigned is finished",
            ),
            discord.SelectOption(
                label="Almost done!",
                value="almost",
                description="Worked today, and task is almost done.",
            ),
            discord.SelectOption(
                label="Not close to finishing.",
                value="not_close",
                description="Worked today, but not near completing the task.",
            ),
            discord.SelectOption(
                label="Skipped",
                value="skipped",
                description="Didn't do anything today.",
            ),
        ]
        super().__init__(
            placeholder="Choose your check-in status...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"checkin_select:{task_id}",
        )
        self.task_id = task_id

    async def callback(self, interaction: discord.Interaction):
        choice_val = self.values[0]

        choice_labels = {
            "done": "did all my work for this task, fully completed!",
            "almost": "did some work today, and should be done soon!",
            "not_close": "did some work today, but probably won't be done soon.",
            "skipped": "didn't do any work today.",
        }
        choice_text = choice_labels.get(choice_val, choice_val)

        content = f"Choice: {choice_text}"

        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO checkins (task_id, user_id, content) VALUES (?, ?, ?)",
                (self.task_id, interaction.user.id, content),
            )
            conn.commit()
        except Exception as e:
            print("DB insert error for checkin:", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        task = get_task_by_id(self.task_id)
        if task:
            captain_id = task["captain_id"]

            thread_message = f"<@{captain_id}>! \nCheck-in from {interaction.user.mention}: {choice_text}\n\n{ctime()}"

            guild_channel_id = get_checkin_channel(interaction.guild_id)

            try:
                if guild_channel_id is not None:
                    channel = await interaction.client.fetch_channel(guild_channel_id)
                    await channel.send(thread_message)
            except Exception as e:
                print("Failed to send checkin to configured channel:", e)
        else:
            return

        try:
            await interaction.response.send_message(
                "Check-in recorded successfully!", ephemeral=True
            )
        except Exception:
            try:
                await interaction.followup.send(
                    "Check-in recorded. Thank you!", ephemeral=True
                )
            except Exception:
                pass


class CheckinChoiceView(discord.ui.View):
    def __init__(self, task_id: int, timeout: Optional[float] = 60.0):
        super().__init__(timeout=timeout)
        self.add_item(CheckinSelect(task_id))


class CheckinView(discord.ui.View):
    def __init__(self, task_id: int):
        super().__init__(timeout=None)
        self.task_id = task_id

        button = discord.ui.Button(
            label="Check-in",
            style=discord.ButtonStyle.primary,
            custom_id=f"task_checkin:{self.task_id}",
        )

        async def button_callback(interaction: discord.Interaction):
            view = CheckinChoiceView(task_id=self.task_id)
            await interaction.response.send_message(
                "What's your progress for today looking like?",
                view=view,
                ephemeral=True,
            )

        button.callback = button_callback
        self.add_item(button)


async def setup(bot: commands.Bot):
    await bot.add_cog(TaskManagement(bot))
