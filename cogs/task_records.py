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

CAPTAIN_ROLE_NAME = "SE"


class TaskManagement(commands.Cog):
    """
    Discord Cog responsible for task assignment, tracking, reminders,
    and check-in management for a guild.

    This cog:
    - Manages task lifecycle (create, update, list, archive, delete)
    - Enforces Team Captain permissions
    - Persists interactive views across bot restarts
    - Periodically reminds users to check in on assigned tasks
    """

    def __init__(self, bot: commands.Bot):
        """
        Initialize the TaskManagement cog.

        - Initializes the database schema
        - Reloads persistent UI views for active tasks
        - Starts the background reminder loop

        Args:
            bot (commands.Bot): The active Discord bot instance.
        """
        self.bot = bot
        init_db()
        self.reload_persistent_views()
        self.checkin_loop.start()

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{__name__} is now ready to micromanage!")

    def reload_persistent_views(self):
        """
        Reload persistent Check-in views from the database.

        Reads all active tasks from the database and re-registers their
        associated persistent `CheckinView` instances with the bot.
        This ensures buttons remain functional after bot restarts.
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM tasks WHERE active = 1")
            rows = cur.fetchall()
            for task_id, name in rows:
                self.bot.add_view(CheckinView(task_id=task_id, name=name))
        except Exception as e:
            print(f"Error reloading persistent views: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def cog_unload(self):
        """
        Called automatically when the cog is unloaded.

        Stops the background check-in reminder loop.
        """
        self.checkin_loop.cancel()

    def within_waking_hours(self):
        """
        Determine whether the current local time is within waking hours.

        Waking hours are defined as:
        - 09:00 AM to 09:00 PM (inclusive)

        Returns:
            bool: True if current time is within waking hours, otherwise False.
        """
        return time(9, 0, 0) <= datetime.now().time() <= time(21, 0, 0)

    def get_query(self, archived, placeholder: Optional[str]):
        """
        Construct a SQL query for fetching tasks.

        Args:
            archived (bool): Whether to query archived tasks instead of active ones.
            placeholder (Optional[str]): SQL placeholder string for filtering by captain IDs.

        Returns:
            str: A parameterized SQL query string.
        """
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
        Check whether the interacting user has the Team Captain role.

        Args:
            interaction (discord.Interaction): The interaction being evaluated.

        Returns:
            bool: True if the user is a guild member with the Team Captain role.
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
        Configure the guild-wide check-in forwarding channel.

        This command:
        - Requires Team Captain permissions
        - Accepts a channel mention or numeric channel ID
        - Updates the stored channel if it differs from the current one

        Args:
            interaction (discord.Interaction): The command interaction.
            channel_id (str): Channel ID or channel mention string.
        """
        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
                "Only team captains can set a checkin channel tasks, sorry! Bug a captain to do their thing.",
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
                f"Please use a valid channel id, or reference one using `#channel`\n{e}",
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
        name="update_assignees",
        description="Updates the assignees within a given task.",
    )
    @app_commands.describe(
        name="Name of the task",
        assignees="Users to assign to this task",
    )
    async def update_assignees(
        self,
        interaction: discord.Interaction,
        name: str,
        assignees: str,
    ):
        """
        Update the assignees for an existing task.

        This updates both:
        - The database task assignment
        - The embedded message in the task thread

        Args:
            interaction (discord.Interaction): The command interaction.
            name (str): Name of the task to update.
            assignees (str): Mentioned users or IDs to assign.
        """
        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
                "Only team captains can change assignees in tasks, you should totally bug one to do it for you 👀",
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

        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT thread_id, due_interval_hours, id FROM tasks WHERE name = ? AND guild_id = ? LIMIT 1
                """,
                (name, guild.id),
            )
            task = cur.fetchone()
        finally:
            conn.close()

        if not task:
            await interaction.followup.send(
                f"No task with the name `{name}` exists in this server.",
                ephemeral=True,
            )
            return
        thread_id, due_interval, task_id = task

        try:
            thread = await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            await interaction.followup.send(
                f"Task thread for `{name}` was not found.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to access this thread... please grant me administrator permissions!",
                ephemeral=True,
            )
            return

        try:
            async for message in thread.history(limit=1, oldest_first=True):
                if message.author == self.bot.user and message.embeds:
                    embed = message.embeds[0]

                    embed.set_field_at(
                        index=0,
                        name="Assignees",
                        value=", ".join(m.mention for m in assignee_members),
                        inline=False,
                    )
                    await message.edit(embed=embed)

                    await interaction.followup.send(
                        f"Task `{name}` assignees updated successfully in {thread.mention}! Now they can work properly :D",
                        ephemeral=True,
                    )
                    return

        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Failed to update assignees: {e}", ephemeral=True
            )

    @app_commands.command(
        name="assign_task",
        description="Create a task thread under the current channel and assigns users to said task.",
    )
    @app_commands.describe(
        name="Short name of the task",
        assignees="Users to assign to this task. Can be one, or many. Format with spaces. (e.g. @user1 @user2 @user3)",
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
        Create a new task and assign users to it.

        This command:
        - Creates a public thread
        - Registers a persistent check-in button
        - Stores task metadata in the database
        - Pins the task overview message

        Args:
            interaction (discord.Interaction): The command interaction.
            name (str): Unique task name.
            assignees (str): Mentioned users or IDs to assign.
            reminder_duration (Optional[int]): Hours between check-in reminders.
        """
        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
                "Only team captains can assign tasks, go and bug someone to do it for you 🥺",
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
                "This command must be used in a text channel or a poll channel.",
                ephemeral=True,
            )
            return

        if interaction.guild_id and get_task_id_by_name(interaction.guild_id, name):
            await interaction.followup.send(
                f"A task named `{name}` already exists. Please choose a unique name so I don't get confused...",
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
                f"Pretty sure that that the task `{name}` already exists... please name it something different!",
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
            f"Task `{name}` created and assigned in {thread.mention}! Woo!",
            ephemeral=True,
        )

    @app_commands.command(
        name="cleanup_tasks",
        description="Mark one or more tasks complete and archive their threads.",
    )
    @app_commands.describe(
        task_names="Semicolon-separated list of task names to complete and clean up. (e.g. task1; task2; task3; task4) or (e.g. task)",
        delete_thread="Whether to delete the task threads. (disabled by default)",
    )
    async def cleanup_task(
        self,
        interaction: discord.Interaction,
        task_names: str,
        delete_thread: Optional[bool] = False,
    ):
        """
        Complete and archive one or more active tasks.

        This command:
        - Marks each task complete in the database
        - Archives and locks each task thread, or deletes it

        Args:
            interaction (discord.Interaction): The command interaction.
            task_names (str): Semicolon-separated list of task names to clean up.
            delete_thread (Optional[bool]): Whether to delete the threads entirely.
        """

        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
                "Only team captains can cleanup tasks. Go ping the captains!",
                ephemeral=True,
            )
            return

        if interaction.guild_id is None:
            await interaction.followup.send(
                "You can only cleanup guild tasks in a guild, silly!",
                ephemeral=True,
            )
            return

        task_list = task_names.split("; ")
        failed_tasks = []
        completed_tasks = []

        for task_name in task_list:
            task_name = task_name.strip()
            if not task_name:
                continue

            task_id = get_task_id_by_name(interaction.guild_id, task_name)
            if task_id is None:
                failed_tasks.append(
                    f'Task "{task_name}" doesn\'t exist, you sure you spelled it right?'
                )
                continue

            task = get_task_by_id(task_id)
            thread_id = task["thread_id"] if task else None

            deleted = complete_task(interaction.guild_id, task_name, delete_thread)
            if not deleted:
                failed_tasks.append(
                    f'Failed to remove task "{task_name}", dw things will still work.'
                )
                continue

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
            elif thread_id:
                thread_channel = self.bot.get_channel(thread_id)
                await thread_channel.delete()

            completed_tasks.append(
                f'Task "{task_name}" marked complete! Assignees will no longer be prompted for check-ins. Woot Woot!'
            )

        summary = []
        if completed_tasks:
            summary.append("\n".join(completed_tasks))
        if failed_tasks:
            summary.append("\n".join(failed_tasks))

        await interaction.followup.send(
            "\n\n".join(summary),
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
        """
        List active or archived tasks in the guild.

        Supports optional filtering by captain ID(s).

        Args:
            interaction (discord.Interaction): The command interaction.
            filter (Optional[str]): Mentioned user IDs to filter by captain.
            archived (Optional[bool]): Whether to list archived tasks.
        """
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send(
                "This command must be run in a server (guild) channel, not in DMs. I mean, threads don't exist there, do they?",
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
                    "There are no open tasks in this guild created by this user(s). What a shame... they should set some up.",
                    ephemeral=True,
                )
            else:
                if archived:
                    await interaction.followup.send(
                        "There are no archived tasks in this guild.", ephemeral=True
                    )
                    return
                await interaction.followup.send(
                    "There are no open tasks in this guild. Woo!!! No work!!",
                    ephemeral=True,
                )
            return

        desc = "active"
        if archived:
            desc = "archived"

        embed = discord.Embed(
            title="Open Tasks",
            description=f"There are currently {len(rows)} {desc} task(s) in this server.",
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
                ", ".join(assignees_list)
                if assignees_list
                else "Nobody... Where is everyone? 😭"
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
        delete_all="Delete all archived tasks.",
    )
    async def delete_archived_tasks(
        self,
        interaction: discord.Interaction,
        task_names: Optional[str] = None,
        delete_all: Optional[bool] = False,
    ):
        """
        Permanently delete archived tasks and their threads.

        Args:
            interaction (discord.Interaction): The command interaction.
            task_names (Optional[str]): Semi-colon separated list of task names.
            delete_all (Optional[bool]): Whether to delete all archived tasks.
        """
        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_is_captain(interaction):
            await interaction.followup.send(
                "Only team captains can delete tasks! Don't go ruining people's productvity now, or else I'll report you! ...or I would if I could",
                ephemeral=True,
            )
            return

        conn = sqlite3.connect(DB_PATH)

        try:
            if not task_names and not delete_all:
                await interaction.followup.send(
                    "Please provide a semi-colon seperated list of task names to delete, or specify delete_all. Otherwise, uh... I can't read.",
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
                    "No matching archived tasks found to delete... so that probably means you spelled it wrong.",
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
                f"""Deleted {deleted_threads} thread(s) and removed {len(archived_tasks)} task(s) from the archive! This cannot be undone, so I hope you know what you were doing.""",
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
        Periodic background task that sends check-in reminders.

        - Runs every 20 minutes
        - Skips execution outside waking hours
        - Sends reminder messages to overdue task threads
        - Updates the next scheduled reminder time
        """
        if not self.within_waking_hours():
            print("Skipping check-in loop: Outside waking hours.")
            return

        current_time = datetime.now()

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
        """
        Await bot readiness before starting the check-in loop.
        """
        await self.bot.wait_until_ready()


class CheckinSelect(discord.ui.Select):
    """
    Dropdown UI component allowing users to submit a daily task check-in.
    """

    def __init__(self, task_id: int, name: str):
        """
        Initialize the check-in dropdown.

        Args:
            task_id (int): Database ID of the task.
            name (str): Human-readable task name.
        """

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
        """
        Handle user selection from the check-in dropdown.

        - Records the check-in in the database
        - Sends a summary embed to the configured check-in channel
        - Acknowledges the user privately

        Args:
            interaction (discord.Interaction): The interaction context.
        """
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
            if conn:
                conn.close()

        task = get_task_by_id(self.task_id)
        thread_id = task.get("thread_id")
        thread = f"<#{thread_id}>"

        if task:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT captain_id FROM tasks WHERE guild_id = ? and id = ?",
                (interaction.guild.id, self.task_id),
            )
            captain = cur.fetchone()[0]

            embed = discord.Embed(
                title=f"New report on Task: {self.name}!",
                description=f"Check-in from {interaction.user.mention}",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Captain:", value=f"<@{captain}>", inline=False)
            embed.add_field(name="Report:", value=choice_text, inline=False)
            embed.add_field(name="Thread:", value=thread, inline=False)
            embed.set_footer(text=ctime())
            guild_channel_id = get_checkin_channel(interaction.guild_id)

        thread = f"<#{task.get('thread_id')}>"
        embed = discord.Embed(
            title=f"New report on Task: {self.name}!",
            description=f"Check-in from {interaction.user.mention}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Report:", value=choice_text, inline=False)
        embed.add_field(name="Thread:", value=thread, inline=False)
        embed.set_footer(text=ctime())
        guild_channel_id = get_checkin_channel(interaction.guild_id)

        if guild_channel_id:
            try:
                channel = await interaction.client.fetch_channel(guild_channel_id)
                await channel.send(embed=embed)
            except Exception as e:
                print("Failed to send checkin to check-in channel:", e)

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
    """
    Temporary view containing the check-in dropdown menu.
    """

    def __init__(self, name, task_id: int, timeout: Optional[float] = 60.0):
        """
        Initialize the check-in choice view.

        Args:
            name (str): Task name.
            task_id (int): Task database ID.
            timeout (Optional[float]): View timeout in seconds.
        """

        super().__init__(timeout=timeout)
        self.name = name
        self.add_item(CheckinSelect(task_id=task_id, name=self.name))


class CheckinView(discord.ui.View):
    """
    Persistent view containing the main 'Check-in' button for a task.
    """

    def __init__(self, name, task_id: int):
        """
        Initialize the persistent check-in button view.

        Args:
            name (str): Task name.
            task_id (int): Task database ID.
        """

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
    """
    Load the TaskManagement cog.

    Args:
        bot (commands.Bot): The Discord bot instance.
    """
    await bot.add_cog(TaskManagement(bot))
