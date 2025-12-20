import asyncio
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
        return time(9, 0, 0) <= datetime.now().time() <= time(21, 0, 0)

    def get_query(self, archived, placeholder: Optional[str]):
        if archived and placeholder:
            return f"""
                    SELECT id, name, thread_id, captain_id, due_interval_hours
                    FROM archived_tasks
                    WHERE guild_id = ? AND captain_id IN ({placeholder})
                    ORDER BY id
                    """
        elif not archived and placeholder:
            return f"""
                    SELECT id, name, thread_id, captain_id, due_interval_hours
                    FROM tasks
                    WHERE guild_id = ? AND active = 1 AND captain_id IN ({placeholder})
                    ORDER BY id
                    """

        if archived:
            return """
                    SELECT id, name, thread_id, captain_id, due_interval_hours
                    FROM archived_tasks
                    WHERE guild_id = ?
                    ORDER BY id
                    """

        return """
                    SELECT id, name, thread_id, captain_id, due_interval_hours
                    FROM tasks
                    WHERE guild_id = ? AND active = 1
                    ORDER BY id
                    """

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
        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
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
            await interaction.followup.send(
                f"Please use a valid channel id...\n{e}",
                ephemeral=True,
            )
            return

        try:
            checkin_channel_id = get_checkin_channel(interaction.guild_id)
        except Exception:
            checkin_channel_id = None

        if checkin_channel_id == channel_id:
            await interaction.followup.send(
                "This channel has already been set as the checkin channel!",
                ephemeral=True,
            )
            return

        try:
            set_checkin_channel(interaction.guild_id, channel_id)
        except Exception as e:
            await interaction.followup.send(
                f"Error setting channel: {e}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
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
        reminder_duration: Optional[int] = 26,
    ):
        """
        Creates a new assignment for a set of given users.

        Behavior:
        - Creates a new thread with a welcome message in the channel that the command was called in
        - Pings all assignees and pins check-in message
        - Saves check-in button to a new view for persistence between reboots
        """
        await interaction.response.defer(ephemeral=True)

        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
                "Only team captains can assign tasks.",
                ephemeral=True,
            )
            return

        try:
            checkin_channel_id = get_checkin_channel(interaction.guild_id)
        except Exception:
            checkin_channel_id = None

        if checkin_channel_id is None:
            await interaction.followup.send(
                "A check-in channel has not been configured yet. Use `/set_checkin_channel` to set it first.",
                ephemeral=True,
            )
            return

        ids = set(int(x) for x in re.findall(r"\d{15,20}", assignees))
        guild = interaction.guild
        assignee_members = []

        if guild:
            for member_id in ids:
                member = guild.get_member(member_id)
                if not member:
                    try:
                        member = await guild.fetch_member(member_id)
                    except discord.HTTPException:
                        continue
                assignee_members.append(member)

        if interaction.channel is None or not isinstance(
            interaction.channel, discord.TextChannel
        ):
            await interaction.followup.send(
                "This command must be used in a text channel.", ephemeral=True
            )
            return

        if interaction.guild_id and get_task_id_by_name(interaction.guild_id, name):
            await interaction.followup.send(
                f"A task named `{name}` already exists. Please choose a unique name.",
                ephemeral=True,
            )
            return

        thread = await interaction.channel.create_thread(
            name=f"Task: {name}", type=discord.ChannelType.public_thread
        )

        task_id = create_task(
            guild_id=interaction.guild_id,
            thread_id=thread.id,
            captain_id=interaction.user.id,
            name=name,
            assignee_ids=[m.id for m in assignee_members],
            due_interval_hours=reminder_duration,
        )

        if task_id is None:
            await thread.delete()
            await interaction.followup.send(
                f"Task `{name}` could not be created due to duplicate names. Use a different name.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Task: {name}",
            description="Use the Check-in button to report your progress today!",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Assignees",
            value=", ".join(m.mention for m in assignee_members),
            inline=False,
        )
        embed.set_footer(
            text=f"Daily check-ins required within {reminder_duration} hours."
        )

        view = CheckinView(task_id=task_id, name=name)
        try:
            first_msg = await thread.send(embed=embed, view=view)
            await asyncio.sleep(1)
            await first_msg.pin()
        except discord.HTTPException as e:
            print(f"Failed to send message in thread {name}: {e}")

        self.bot.add_view(view)
        await interaction.followup.send(
            f"Task `{name}` created and assigned in {thread.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="cleanup_task", description="Mark a task complete and archive its thread."
    )
    @app_commands.describe(
        task_name="Name of the task to complete and clean up.",
        delete_thread="Whether to delete the task thread. (disabled by default)",
    )
    async def cleanup_task(
        self,
        interaction: discord.Interaction,
        task_name: str,
        delete_thread: Optional[bool] = False,
    ):
        """
        Closes an active task.

        Behavior:
        - Locks and archives the thread associated with a task
        - Removes relevant information from the database
        """

        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
                "Only team captains can complete tasks.",
                ephemeral=True,
            )
            return

        if interaction.guild_id is None:
            await interaction.followup.send(
                "You can only cleanup guild tasks in a guild, silly!",
                ephemeral=True,
            )
            return

        task_id = get_task_id_by_name(interaction.guild_id, task_name)
        if task_id is None:
            await interaction.followup.send(
                f'The task "{task_name}" doesn\'t exist here... are you sure you spelled it right?',
                ephemeral=True,
            )
            return

        task = get_task_by_id(task_id)
        thread_id = task["thread_id"] if task else None

        deleted = complete_task(interaction.guild_id, task_name, delete_thread)
        if not deleted:
            await interaction.followup.send(
                f'Failed to remove task "{task_name}".',
                ephemeral=True,
            )
            return

        if not delete_thread and thread_id:
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
        else:
            thread_channel = self.bot.get_channel(thread_id)
            await thread_channel.delete()

        await interaction.followup.send(
            f'Task "{task_name}" marked complete. Assignees will no longer be prompted for check-ins.',
            ephemeral=True,
        )

    @app_commands.command(
        name="list_tasks",
        description="List all tasks in this guild.",
    )
    @app_commands.describe(
        filter="Specify user(s) to list open threads for! A simple filter in case you don't want your screen cluttered.",
        archived="Whether or not list archived, or active tasks",
    )
    async def list_tasks(
        self,
        interaction: discord.Interaction,
        filter: Optional[str],
        archived: Optional[bool] = False,
    ):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send(
                "This command must be run in a server (guild) channel, not in DMs.",
                ephemeral=True,
            )
            return

        try:
            if filter:
                id = set(int(x) for x in re.findall(r"\d{15,20}", filter))
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                captain_ids = tuple(id)

                placeholders = ", ".join("?" for _ in captain_ids)
                query = self.get_query(archived=archived, placeholder=placeholders)

                cur.execute(query, (guild_id, *captain_ids))
                rows = cur.fetchall()
            else:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()

                query = self.get_query(archived=archived, placeholder=None)

                cur.execute(query, (guild_id,))
                rows = cur.fetchall()

        except Exception as e:
            await interaction.followup.send(
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
            if filter:
                await interaction.followup.send(
                    "There are no open tasks in this guild created by this user(s).",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "There are no open tasks in this guild.", ephemeral=True
                )
            return

        desc = "active"
        if archived:
            desc = "archived"

        embed = discord.Embed(
            title="Open Tasks",
            description=f"Currently {len(rows)} {desc} task(s) in this server.",
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

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="delete_archived_tasks",
        description="Delete specific or all archived tasks.",
    )
    @app_commands.describe(
        task_names="Semi-colon seperated list of archived task names to delete (e.g., task1; task2).",
        delete_all="Delete all archived tasks. Defaults to false if task_names is provided.",
    )
    async def delete_archived_tasks(
        self,
        interaction: discord.Interaction,
        task_names: Optional[str] = None,
        delete_all: Optional[bool] = False,
    ):
        """
        Deletes specific or all threads associated with archived tasks in the archived_tasks table.
        """
        await interaction.response.defer(ephemeral=True)

        conn = sqlite3.connect(DB_PATH)

        try:
            if not task_names and not delete_all:
                await interaction.followup.send(
                    "Please provide a semi-colon seperated list of task names to delete.",
                    ephemeral=True,
                )
                return

            if delete_all:
                cur = conn.cursor()
                cur.execute(
                    "SELECT thread_id, name FROM archived_tasks WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                archived_tasks = cur.fetchall()
                task_names_to_delete = [task[1] for task in archived_tasks]
            else:
                task_names_to_delete = [name.strip() for name in task_names.split(",")]
                cur = conn.cursor()
                placeholders = "; ".join("?" for _ in task_names_to_delete)
                cur.execute(
                    f"SELECT thread_id, name FROM archived_tasks WHERE guild_id = ? AND name IN ({placeholders})",
                    (interaction.guild_id, *task_names_to_delete),
                )
                archived_tasks = cur.fetchall()

            if not archived_tasks:
                await interaction.followup.send(
                    "No matching archived tasks found to delete.",
                    ephemeral=True,
                )
                return

            deleted_threads = 0
            for thread_id, name in archived_tasks:
                try:
                    thread = self.bot.get_channel(thread_id)
                    if thread is None:
                        try:
                            thread = await self.bot.fetch_channel(thread_id)
                        except discord.HTTPException:
                            continue

                    if thread:
                        await thread.delete()
                        deleted_threads += 1
                except discord.HTTPException as e:
                    print(f"Failed to delete thread {thread_id}: {e}")

            cur.execute(
                "DELETE FROM archived_tasks WHERE guild_id = ? AND name IN ({})".format(
                    ", ".join("?" for _ in task_names_to_delete)
                ),
                (interaction.guild_id, *task_names_to_delete),
            )
            conn.commit()

            await interaction.followup.send(
                f"Deleted {deleted_threads} thread(s) and removed {len(archived_tasks)} task(s) from the archive.",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.followup.send(
                f"Failed to delete archived tasks: {e}",
                ephemeral=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @tasks.loop(minutes=20)
    async def checkin_loop(self):
        """
        Task loop to check which threads need reminders and send notifications.
        """
        if not self.within_waking_hours():
            print("Skipping check-in loop: Outside waking hours.")
            return

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
                thread = await self.bot.fetch_channel(thread_id)

                await thread.send(
                    "Don't forget to send in today's check-in if you haven't already!"
                )
                await asyncio.sleep(1)

                next_check_time = current_time + timedelta(hours=due_interval_hours)
                with sqlite3.connect(DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE tasks SET next_check_time = ? WHERE thread_id = ?",
                        (next_check_time, thread_id),
                    )
                    conn.commit()
            except discord.HTTPException as e:
                print(f"Failed to send reminder to thread {thread_id}: {e}")

    @checkin_loop.before_loop
    async def before_checkin_loop(self):
        await self.bot.wait_until_ready()


class CheckinSelect(discord.ui.Select):
    def __init__(self, task_id: int, name: str):
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
        self.name = name

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
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
            embed = discord.Embed(
                title=f"New report on Task: {self.name}!",
                description=f" Check-in from {interaction.user.mention}",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Report:", value=choice_text, inline=False)
            embed.set_footer(text=ctime())
            guild_channel_id = get_checkin_channel(interaction.guild_id)

            try:
                if guild_channel_id is not None:
                    channel = await interaction.client.fetch_channel(guild_channel_id)
                    await channel.send(embed=embed)
            except Exception as e:
                print("Failed to send checkin to configured channel:", e)
        else:
            return

        try:
            await interaction.response.followup(
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
    def __init__(self, name, task_id: int, timeout: Optional[float] = 60.0):
        super().__init__(timeout=timeout)
        self.name = name
        self.add_item(CheckinSelect(task_id=task_id, name=self.name))


class CheckinView(discord.ui.View):
    def __init__(self, name, task_id: int):
        super().__init__(timeout=None)
        self.task_id = task_id
        self.name = name

        button = discord.ui.Button(
            label="Check-in",
            style=discord.ButtonStyle.primary,
            custom_id=f"task_checkin:{self.task_id}",
        )

        async def button_callback(interaction: discord.Interaction):
            view = CheckinChoiceView(task_id=self.task_id, name=name)
            await interaction.response.send_message(
                "What's your progress for today looking like?",
                view=view,
                ephemeral=True,
            )

        button.callback = button_callback
        self.add_item(button)


async def setup(bot: commands.Bot):
    await bot.add_cog(TaskManagement(bot))
