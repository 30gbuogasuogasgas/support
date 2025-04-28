import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context
from discord.ui import Button, View
import asyncio
import datetime
import logging
import json
import os
import re
import io
from typing import Optional, Union, Dict, List

 
# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("modmail.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('ModmailBot')

# Configure intents
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.members = True
intents.guilds = True

# Config
CONFIG = {
    "TOKEN": os.getenv("TOKEN"),
    "GUILD_ID": 978817733846253598,  # Replace with your server ID
    "STAFF_ROLES": ["Admin", "Moderator", "On-Duty Support"],
    "TICKET_CATEGORY": "MODMAIL",
    "LOG_CHANNEL": "modmail-logs",
    "BOT_STATUS": "DMs for support",
    "EMBED_COLOR": 0x3498db,  # Blue color
    "SUPPORT_EMBED_COLOR": 0x2ecc71,  # Green color
    "DEV_EMBED_COLOR": 0x9b59b6,  # Purple color
    "ERROR_COLOR": 0xe74c3c,  # Red color
    "TICKET_CLOSE_CONFIRMATION": True,
    "ANONYMOUS_REPLIES": False,
    "AUTO_CLOSE_TIME": 48,  # Hours before auto-closing inactive tickets
    "TICKET_LIMIT_PER_USER": 1,
    "WELCOME_MESSAGE_COOLDOWN": 300,  # Seconds (5 minutes) between welcome messages
}

# Create bot instance
bot = commands.Bot(command_prefix="!", intents=intents)

# Data storage
active_tickets = {}
blacklisted_users = set()
ticket_logs = {}
user_welcome_timestamps = {}  # Track when users received welcome messages
ticket_attachments = {}  # Track attachments by message ID for reactions

# Utility functions
def save_data():
    """Save persistent data to disk"""
    data = {
        "blacklisted_users": list(blacklisted_users),
        "ticket_logs": ticket_logs,
        "user_welcome_timestamps": {str(user_id): timestamp for user_id, timestamp in user_welcome_timestamps.items()}
    }
    with open("modmail_data.json", "w") as f:
        json.dump(data, f, indent=4)

def load_data():
    """Load persistent data from disk"""
    global blacklisted_users, ticket_logs, user_welcome_timestamps
    try:
        if os.path.exists("modmail_data.json"):
            with open("modmail_data.json", "r") as f:
                data = json.load(f)
                blacklisted_users = set(data.get("blacklisted_users", []))
                ticket_logs = data.get("ticket_logs", {})
                user_welcome_timestamps = {int(user_id): timestamp for user_id, timestamp in data.get("user_welcome_timestamps", {}).items()}
    except Exception as e:
        logger.error(f"Error loading data: {e}")

# Ticket creation
async def create_ticket(user: discord.User, category_name: str, message_content: str, guild: discord.Guild):
    """Create a new ticket channel in the server"""
    # Check if user has reached ticket limit
    if user.id in active_tickets and len(active_tickets[user.id]) >= CONFIG["TICKET_LIMIT_PER_USER"]:
        return "You've reached the maximum number of open tickets. Please close existing tickets before creating a new one."
    
    # Check if user is blacklisted
    if user.id in blacklisted_users:
        return "You are currently blacklisted from creating tickets. Please contact an administrator if you believe this is an error."

    try:
        # Find or create the Modmail category
        category = discord.utils.get(guild.categories, name=CONFIG["TICKET_CATEGORY"])
        if not category:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True),
            }
            category = await guild.create_category(CONFIG["TICKET_CATEGORY"], overwrites=overwrites)
            logger.info(f"Created new {CONFIG['TICKET_CATEGORY']} category")

        # Set up permissions for staff roles
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        
        # Add permissions for staff roles
        for role_name in CONFIG["STAFF_ROLES"]:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        
        # Create unique channel name with timestamp
        timestamp = discord.utils.utcnow().strftime('%Y%m%d-%H%M')
        clean_username = re.sub(r'[^a-zA-Z0-9]', '', user.name)[:10]
        channel_name = f"{clean_username}-{category_name.lower()}-{timestamp}"
        
        # Create the channel
        channel = await category.create_text_channel(channel_name, overwrites=overwrites)
        
        # Initialize ticket in active_tickets
        if user.id not in active_tickets:
            active_tickets[user.id] = {}
            
        active_tickets[user.id][channel.id] = {
            "channel": channel,
            "category": category_name,
            "created_at": discord.utils.utcnow(),
            "last_activity": discord.utils.utcnow(),
            "messages": [],
            "closed": False,
            "staff_typing": False,
            "user_typing": False
        }

        # Create initial ticket embed with user avatar and formatted info
        embed_color = CONFIG["SUPPORT_EMBED_COLOR"] if category_name == "Support" else CONFIG["DEV_EMBED_COLOR"]
        initial_embed = discord.Embed(
            title=f"{category_name} Ticket",
            description=f"User: {user.mention}\nInitial Message: {message_content}",
            color=embed_color,
            timestamp=discord.utils.utcnow()
        )
        initial_embed.set_thumbnail(url=user.display_avatar.url)
        initial_embed.add_field(name="Status", value="Open", inline=True)
        initial_embed.add_field(name="Category", value=category_name, inline=True)
        initial_embed.add_field(name="User Info", value=f"ID: {user.id}\nCreated: <t:{int(user.created_at.timestamp())}:R>", inline=False)
        initial_embed.set_footer(text=f"Ticket ID: {channel.id} • Created: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Add ticket controls
        class TicketControls(View):
            def __init__(self):
                super().__init__(timeout=None)
            
            @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
            async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await close_ticket(interaction, channel, user)
            
            @discord.ui.button(label="Blacklist User", style=discord.ButtonStyle.secondary, custom_id="blacklist_user")
            async def blacklist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                # Check if user has permission
                if not any(role.name in CONFIG["STAFF_ROLES"] for role in interaction.user.roles):
                    await interaction.response.send_message("You don't have permission to blacklist users.", ephemeral=True)
                    return
                
                # Blacklist user
                blacklisted_users.add(user.id)
                save_data()
                await interaction.response.send_message(f"User {user.mention} has been blacklisted from creating tickets.", ephemeral=False)
            
            @discord.ui.button(label="Archive", style=discord.ButtonStyle.secondary, custom_id="archive_ticket")
            async def archive_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await interaction.response.send_message("Archiving ticket...", ephemeral=True)
                try:
                    await channel.edit(archived=True)
                    # Mark as closed but keep in logs
                    active_tickets[user.id][channel.id]["closed"] = True
                    save_data()
                except Exception as e:
                    await interaction.followup.send(f"Error archiving channel: {str(e)}", ephemeral=True)
                    
            @discord.ui.button(label="Transfer", style=discord.ButtonStyle.primary, custom_id="transfer_ticket")
            async def transfer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                # Create a dropdown for department selection
                class DepartmentSelect(discord.ui.Select):
                    def __init__(self):
                        options = [
                            discord.SelectOption(label="Support", description="General support issues"),
                            discord.SelectOption(label="Development", description="Development related issues"),
                            discord.SelectOption(label="Billing", description="Payment and subscription issues"),
                            discord.SelectOption(label="Urgent", description="High priority issues")
                        ]
                        super().__init__(placeholder="Select a department", options=options)
                    
                    async def callback(self, interaction: discord.Interaction):
                        new_category = self.values[0]
                        
                        # Update channel name
                        timestamp = channel.name.split('-')[-1]
                        clean_username = re.sub(r'[^a-zA-Z0-9]', '', user.name)[:10]
                        new_channel_name = f"{clean_username}-{new_category.lower()}-{timestamp}"
                        
                        try:
                            await channel.edit(name=new_channel_name)
                            # Update ticket data
                            active_tickets[user.id][channel.id]["category"] = new_category
                            
                            # Send confirmation
                            transfer_embed = discord.Embed(
                                title="Ticket Transferred",
                                description=f"This ticket has been transferred to the {new_category} department.",
                                color=CONFIG["EMBED_COLOR"],
                                timestamp=discord.utils.utcnow()
                            )
                            await channel.send(embed=transfer_embed)
                            
                            # Notify user
                            user_embed = discord.Embed(
                                title="Ticket Transferred",
                                description=f"Your ticket has been transferred to the {new_category} department.",
                                color=CONFIG["EMBED_COLOR"],
                                timestamp=discord.utils.utcnow()
                            )
                            user_embed.set_footer(text=f"Support System • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
                            await user.send(embed=user_embed)
                            
                            await interaction.response.send_message(f"Ticket transferred to {new_category} department.", ephemeral=True)
                        except Exception as e:
                            await interaction.response.send_message(f"Error transferring ticket: {str(e)}", ephemeral=True)
                
                # Create the view with the dropdown
                class DepartmentView(View):
                    def __init__(self):
                        super().__init__(timeout=60)
                        self.add_item(DepartmentSelect())
                
                await interaction.response.send_message("Select a department to transfer this ticket to:", view=DepartmentView(), ephemeral=True)

        ticket_controls = TicketControls()
        control_message = await channel.send(embed=initial_embed, view=ticket_controls)
        await control_message.pin()
        
        # Send welcome message to user
        user_embed = discord.Embed(
            title=f"Your {category_name} Ticket Has Been Created",
            description="Our staff team will respond to your inquiry shortly. Please provide any additional information that may help us assist you.",
            color=embed_color,
            timestamp=discord.utils.utcnow()
        )
        user_embed.set_footer(text=f"Support System • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
        await user.send(embed=user_embed)
        
        # Log ticket creation
        await log_action(guild, f"Ticket created by {user.name} ({user.id})", category_name, message_content)
        
        return f"Your {category_name} ticket has been created. Our staff team will respond shortly."

    except Exception as e:
        logger.error(f"Error creating ticket: {str(e)}")
        return "An error occurred while creating your ticket. Please try again later."

async def close_ticket(interaction, channel, user):
    """Close a ticket with confirmation"""
    if CONFIG["TICKET_CLOSE_CONFIRMATION"]:
        class ConfirmClose(View):
            def __init__(self):
                super().__init__(timeout=30.0)
                
            @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger)
            async def confirm(self, confirm_interaction: discord.Interaction, button: discord.ui.Button):
                await actually_close_ticket(confirm_interaction, channel, user)
                
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, cancel_interaction: discord.Interaction, button: discord.ui.Button):
                await cancel_interaction.response.send_message("Ticket close cancelled.", ephemeral=True)
                self.stop()
                
        await interaction.response.send_message("Are you sure you want to close this ticket?", view=ConfirmClose(), ephemeral=True)
    else:
        await actually_close_ticket(interaction, channel, user)

async def actually_close_ticket(interaction, channel, user):
    """Actually close the ticket after confirmation"""
    try:
        # Create transcript
        transcript = await create_transcript(channel, user)
        
        # Send closing message to channel
        closing_embed = discord.Embed(
            title="Ticket Closed",
            description=f"This ticket has been closed by {interaction.user.mention}.",
            color=CONFIG["ERROR_COLOR"],
            timestamp=discord.utils.utcnow()
        )
        await channel.send(embed=closing_embed)
        
        # Send notification to user with transcript
        try:
            user_embed = discord.Embed(
                title="Ticket Closed",
                description="Your ticket has been closed by a staff member. If you need further assistance, you can create a new ticket.",
                color=CONFIG["ERROR_COLOR"],
                timestamp=discord.utils.utcnow()
            )
            
            # Create an in-memory file with the transcript
            transcript_file = discord.File(io.StringIO(transcript), filename=f"transcript-{channel.name}.txt")
            
            await user.send(embed=user_embed, file=transcript_file)
        except Exception as e:
            logger.error(f"Could not send closing notification to user: {str(e)}")
        
        # Update active_tickets
        if user.id in active_tickets and channel.id in active_tickets[user.id]:
            ticket_data = active_tickets[user.id][channel.id]
            # Store in logs
            if str(user.id) not in ticket_logs:
                ticket_logs[str(user.id)] = []
                
            ticket_logs[str(user.id)].append({
                "channel_name": channel.name,
                "category": ticket_data["category"],
                "created_at": ticket_data["created_at"].isoformat(),
                "closed_at": discord.utils.utcnow().isoformat(),
                "closed_by": str(interaction.user.id),
                "message_count": len(ticket_data["messages"])
            })
            
            # Remove from active tickets
            del active_tickets[user.id][channel.id]
            if not active_tickets[user.id]:
                del active_tickets[user.id]
                
            save_data()
            
        # Log the action
        guild = channel.guild
        await log_action(guild, f"Ticket closed by {interaction.user.name} ({interaction.user.id})", "Ticket Closure", "")
        
        # Delete the channel
        await interaction.response.send_message("Ticket closed successfully. This channel will be deleted in 5 seconds.", ephemeral=True)
        await asyncio.sleep(5)
        await channel.delete()
        
    except Exception as e:
        await interaction.response.send_message(f"Error closing ticket: {str(e)}", ephemeral=True)
        logger.error(f"Error closing ticket: {str(e)}")

async def create_transcript(channel, user):
    """Create a transcript of the ticket conversation"""
    transcript = f"Transcript for ticket: {channel.name}\n"
    transcript += f"User: {user.name} (ID: {user.id})\n"
    transcript += f"Created at: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
    transcript += "-" * 50 + "\n\n"
    
    # Get all messages in the channel
    messages = []
    async for message in channel.history(limit=None, oldest_first=True):
        messages.append(message)
    
    for message in messages:
        # Skip system messages or pinned control message
        if message.author.bot and len(message.embeds) > 0 and message.pinned:
            continue
            
        timestamp = message.created_at.strftime('%Y-%m-%d %H:%M:%S')
        author = f"{message.author.name} (Staff)" if any(role.name in CONFIG["STAFF_ROLES"] for role in message.author.roles) else message.author.name
        
        transcript += f"[{timestamp}] {author}:\n"
        
        # Add message content
        if message.content:
            transcript += f"{message.content}\n"
            
        # Add embed content
        if message.embeds:
            for embed in message.embeds:
                if embed.description:
                    transcript += f"{embed.description}\n"
                for field in embed.fields:
                    transcript += f"{field.name}: {field.value}\n"
        
        # Add attachment info
        if message.attachments:
            transcript += "Attachments:\n"
            for attachment in message.attachments:
                transcript += f"- {attachment.filename} ({attachment.url})\n"
        
        transcript += "\n"
    
    return transcript

async def log_action(guild, action, category, details):
    """Log actions to the designated log channel"""
    log_channel_name = CONFIG["LOG_CHANNEL"]
    log_channel = discord.utils.get(guild.text_channels, name=log_channel_name)
    
    if not log_channel:
        # Create log channel if it doesn't exist
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        
        # Add permissions for staff roles
        for role_name in CONFIG["STAFF_ROLES"]:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True)
                
        log_channel = await guild.create_text_channel(log_channel_name, overwrites=overwrites)
    
    log_embed = discord.Embed(
        title=f"{category} Log",
        description=action,
        color=CONFIG["EMBED_COLOR"],
        timestamp=discord.utils.utcnow()
    )
    
    if details:
        log_embed.add_field(name="Details", value=details)
        
    await log_channel.send(embed=log_embed)

# Button Views
class SupportTicketView(View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Support", style=discord.ButtonStyle.success, custom_id="support_ticket")
    async def support_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = bot.get_guild(CONFIG["GUILD_ID"])
        if not guild:
            guild = interaction.guild
            
        response = await create_ticket(interaction.user, "Support", "Support ticket requested", guild)
        await interaction.followup.send(response, ephemeral=True)
        
    @discord.ui.button(label="Development", style=discord.ButtonStyle.primary, custom_id="dev_ticket")
    async def dev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = bot.get_guild(CONFIG["GUILD_ID"])
        if not guild:
            guild = interaction.guild
            
        response = await create_ticket(interaction.user, "Development", "Development ticket requested", guild)
        await interaction.followup.send(response, ephemeral=True)
        
    @discord.ui.button(label="Billing", style=discord.ButtonStyle.secondary, custom_id="billing_ticket")
    async def billing_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = bot.get_guild(CONFIG["GUILD_ID"])
        if not guild:
            guild = interaction.guild
            
        response = await create_ticket(interaction.user, "Billing", "Billing ticket requested", guild)
        await interaction.followup.send(response, ephemeral=True)
        
    @discord.ui.button(label="Urgent", style=discord.ButtonStyle.danger, custom_id="urgent_ticket")
    async def urgent_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = bot.get_guild(CONFIG["GUILD_ID"])
        if not guild:
            guild = interaction.guild
            
        response = await create_ticket(interaction.user, "Urgent", "URGENT ticket requested", guild)
        await interaction.followup.send(response, ephemeral=True)

async def send_welcome_message(user: discord.User):
    """Send welcome message with ticket buttons"""
    current_time = discord.utils.utcnow().timestamp()
    
    # Check if user has received a welcome message recently
    if user.id in user_welcome_timestamps:
        last_welcome = user_welcome_timestamps[user.id]
        if current_time - last_welcome < CONFIG["WELCOME_MESSAGE_COOLDOWN"]:
            # Don't spam them with welcome messages
            return
    
    # Create welcome embed
    welcome_embed = discord.Embed(
        title="Support Ticket System",
        description="Welcome to our support system! Please select a ticket category below to create a new ticket.",
        color=CONFIG["EMBED_COLOR"]
    )
    welcome_embed.set_thumbnail(url=bot.user.display_avatar.url)
    welcome_embed.add_field(name="Support", value="General help and assistance", inline=True)
    welcome_embed.add_field(name="Development", value="Technical issues and development requests", inline=True)
    welcome_embed.add_field(name="Billing", value="Questions about payments and subscriptions", inline=True)
    welcome_embed.add_field(name="Urgent", value="Critical issues requiring immediate attention", inline=True)
    welcome_embed.add_field(name="Instructions", value="Click one of the buttons below to create your ticket.", inline=False)
    welcome_embed.set_footer(text=f"Support System • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Send embed with buttons
    view = SupportTicketView()
    await user.send(embed=welcome_embed, view=view)
    
    # Update timestamp
    user_welcome_timestamps[user.id] = current_time
    save_data()

# Event handlers
@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    
    # Load saved data
    load_data()
    
    # Set bot status
    activity = discord.Activity(type=discord.ActivityType.watching, name=CONFIG["BOT_STATUS"])
    await bot.change_presence(activity=activity)
    
    # Start auto-close task
    if CONFIG["AUTO_CLOSE_TIME"] > 0:
        bot.loop.create_task(auto_close_tickets())
        
    logger.info('Bot is ready!')

async def auto_close_tickets():
    """Automatically close inactive tickets"""
    while True:
        try:
            current_time = discord.utils.utcnow()
            to_close = []
            
            # Find inactive tickets
            for user_id, user_tickets in active_tickets.items():
                for channel_id, ticket_data in user_tickets.items():
                    if ticket_data["closed"]:
                        continue
                        
                    last_activity = ticket_data["last_activity"]
                    hours_inactive = (current_time - last_activity).total_seconds() / 3600
                    
                    if hours_inactive >= CONFIG["AUTO_CLOSE_TIME"]:
                        to_close.append((user_id, channel_id))
            
            # Close inactive tickets
            for user_id, channel_id in to_close:
                try:
                    channel = active_tickets[user_id][channel_id]["channel"]
                    user = await bot.fetch_user(user_id)
                    
                    # Send auto-close message
                    auto_close_embed = discord.Embed(
                        title="Ticket Auto-Closed",
                        description=f"This ticket has been automatically closed due to {CONFIG['AUTO_CLOSE_TIME']} hours of inactivity.",
                        color=CONFIG["ERROR_COLOR"],
                        timestamp=discord.utils.utcnow()
                    )
                    await channel.send(embed=auto_close_embed)
                    
                    # Create transcript
                    transcript = await create_transcript(channel, user)
                    
                    # Notify user
                    try:
                        user_embed = discord.Embed(
                            title="Ticket Auto-Closed",
                            description=f"Your ticket has been automatically closed due to {CONFIG['AUTO_CLOSE_TIME']} hours of inactivity. If you need further assistance, you can create a new ticket.",
                            color=CONFIG["ERROR_COLOR"],
                            timestamp=discord.utils.utcnow()
                        )
                        
                        # Create an in-memory file with the transcript
                        transcript_file = discord.File(io.StringIO(transcript), filename=f"transcript-{channel.name}.txt")
                        
                        await user.send(embed=user_embed, file=transcript_file)
                    except:
                        logger.error(f"Could not send auto-close notification to user {user_id}")
                    
                    # Update logs and remove from active tickets
                    ticket_data = active_tickets[user_id][channel_id]
                    if str(user_id) not in ticket_logs:
                        ticket_logs[str(user_id)] = []
                        
                    ticket_logs[str(user_id)].append({
                        "channel_name": channel.name,
                        "category": ticket_data["category"],
                        "created_at": ticket_data["created_at"].isoformat(),
                        "closed_at": current_time.isoformat(),
                        "closed_by": "auto-close",
                        "message_count": len(ticket_data["messages"])
                    })
                    
                    # Remove from active tickets
                    del active_tickets[user_id][channel_id]
                    if not active_tickets[user_id]:
                        del active_tickets[user_id]
                        
                    save_data()
                    
                    # Delete channel
                    await channel.delete()
                    
                except Exception as e:
                    logger.error(f"Error auto-closing ticket: {str(e)}")
            
            # Sleep before next check (15 minutes)
            await asyncio.sleep(900)
            
        except Exception as e:
            logger.error(f"Error in auto-close task: {str(e)}")
            await asyncio.sleep(300)  # Shorter sleep on error

# Typing indicator events
@bot.event
async def on_typing(channel, user, when):
    """Handle typing indicators"""
    # Skip if user is a bot
    if user.bot:
        return
        
    # Check if it's a DM channel and user has an active ticket
    if isinstance(channel, discord.DMChannel) and user.id in active_tickets:
        for channel_id, ticket_data in active_tickets[user.id].items():
            if not ticket_data["closed"] and not ticket_data["user_typing"]:
                ticket_channel = ticket_data["channel"]
                ticket_data["user_typing"] = True
                
                # Send typing indicator to ticket channel
                async with ticket_channel.typing():
                    # Wait for a bit then reset
                    await asyncio.sleep(5)
                    ticket_data["user_typing"] = False
                    
    # Check if it's a ticket channel and update typing status for staff
    else:
        ticket_owner_id = None
        for user_id, user_tickets in active_tickets.items():
            for channel_id, ticket_data in user_tickets.items():
                if channel_id == channel.id and not ticket_data["closed"]:
                    ticket_owner_id = user_id
                    if not ticket_data["staff_typing"]:
                        ticket_data["staff_typing"] = True
                        
                        # Get user
                        ticket_owner = await bot.fetch_user(ticket_owner_id)
                        
                        # Send typing indicator to user DM
                        try:
                            async with ticket_owner.typing():
                                # Wait for a bit then reset
                                await asyncio.sleep(5)
                                ticket_data["staff_typing"] = False
                        except:
                            ticket_data["staff_typing"] = False
                            
                    break
            if ticket_owner_id:
                break

@bot.event
async def on_message(message):
    # Ignore messages from bots (except our own)
    if message.author.bot and message.author.id != bot.user.id:
        return
    
    # Check for command prefix first and process commands without forwarding
    if not message.author.bot and message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return
    # Process DM messages
    if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
        # Check for blacklisted users
        if message.author.id in blacklisted_users:
            blacklist_embed = discord.Embed(
                title="Access Denied",
                description="You are currently blacklisted from using the ticket system. Please contact an administrator if you believe this is an error.",
                color=CONFIG["ERROR_COLOR"]
            )
            await message.channel.send(embed=blacklist_embed)
            return
        
        # Check for welcome message trigger (e.g., first contact)
        if not any(message.author.id in active_tickets for _ in [1]):
            await send_welcome_message(message.author)
            return
                
        # Check if user has active tickets and forward message
        if message.author.id in active_tickets:
            # Find all active tickets for this user
            active_user_tickets = active_tickets[message.author.id]
            
            # If multiple tickets, ask which one to send to
            if len(active_user_tickets) > 1 and not message.reference:
                options = []
                for channel_id, ticket_data in active_user_tickets.items():
                    if not ticket_data["closed"]:
                        channel = ticket_data["channel"]
                        options.append(discord.SelectOption(
                            label=f"{ticket_data['category']} Ticket",
                            description=f"Created {ticket_data['created_at'].strftime('%Y-%m-%d')}",
                            value=str(channel_id)
                        ))
                
                if options:
                    class TicketSelect(discord.ui.Select):
                        def __init__(self):
                            super().__init__(placeholder="Select a ticket to reply to", options=options)
                        
                        async def callback(self, interaction: discord.Interaction):
                            selected_channel_id = int(self.values[0])
                            if selected_channel_id in active_user_tickets:
                                ticket_data = active_user_tickets[selected_channel_id]
                                ticket_channel = ticket_data["channel"]
                                
                                # Forward the message to the selected channel
                                await forward_to_channel(message, ticket_channel)
                                
                                # Send confirmation to user
                                await interaction.response.send_message(f"Your message has been sent to your {ticket_data['category']} ticket.", ephemeral=True)
                    
                    view = View()
                    view.add_item(TicketSelect())
                    select_embed = discord.Embed(
                        title="Multiple Active Tickets",
                        description="You have multiple open tickets. Please select which ticket you want to send this message to:",
                        color=CONFIG["EMBED_COLOR"]
                    )
                    await message.channel.send(embed=select_embed, view=view)
                    return
            
            # Forward to first active ticket if only one or no selection needed
            for channel_id, ticket_data in active_user_tickets.items():
                if not ticket_data["closed"]:
                    await forward_to_channel(message, ticket_data["channel"])
                    break
        else:
            # No active tickets, send welcome message
            await send_welcome_message(message.author)
            
    # Process ticket channel messages (from staff to user)
    elif not isinstance(message.channel, discord.DMChannel) and not message.author.bot:
        # Check if this is a ticket channel
        ticket_owner_id = None
        ticket_data = None
        
        for user_id, user_tickets in active_tickets.items():
            for channel_id, ticket_info in user_tickets.items():
                if channel_id == message.channel.id and not ticket_info["closed"]:
                    ticket_owner_id = user_id
                    ticket_data = ticket_info
                    break
            if ticket_owner_id:
                break
                
        # Forward the message to the ticket owner if found
        if ticket_owner_id and ticket_data:
            # Update last activity timestamp
            ticket_data["last_activity"] = discord.utils.utcnow()
            
            # Store the message in ticket data
            ticket_data["messages"].append({
                "author_id": message.author.id,
                "content": message.content,
                "timestamp": discord.utils.utcnow().isoformat(),
                "attachments": [a.url for a in message.attachments]
            })
            
            try:
                # Get the ticket owner
                user = await bot.fetch_user(ticket_owner_id)
                
                # Create and send the message to user
                if CONFIG["ANONYMOUS_REPLIES"]:
                    author_name = "Staff"
                    author_avatar = bot.user.display_avatar.url
                else:
                    author_name = message.author.name
                    author_avatar = message.author.display_avatar.url
                
                # Create embed for the message
                embed = discord.Embed(
                    description=message.content or "*No message content*",
                    color=CONFIG["EMBED_COLOR"],
                    timestamp=discord.utils.utcnow()
                )
                embed.set_author(name=author_name, icon_url=author_avatar)
                embed.set_footer(text=f"Support System • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Handle attachments
                files = []
                for attachment in message.attachments:
                    try:
                        file_data = await attachment.read()
                        file = discord.File(io.BytesIO(file_data), filename=attachment.filename)
                        files.append(file)
                        
                        # Add attachment info to embed
                        embed.add_field(name="Attachment", value=f"[{attachment.filename}]({attachment.url})", inline=False)
                    except Exception as e:
                        logger.error(f"Error processing attachment: {str(e)}")
                
                # Send the message
                user_message = await user.send(embed=embed, files=files if files else None)
                
                # Add reaction buttons for quick replies
                await message.add_reaction("✅")  # Confirmation that message was sent
                
            except Exception as e:
                logger.error(f"Error forwarding message to user: {str(e)}")
                await message.channel.send(f"Error sending message to user: {str(e)}")
                
    # Process commands
    await bot.process_commands(message)

async def forward_to_channel(message, channel):
    """Forward a user's DM to the ticket channel"""
    try:
        # Find the ticket data
        ticket_data = None
        for user_id, user_tickets in active_tickets.items():
            for channel_id, ticket_info in user_tickets.items():
                if channel_id == channel.id:
                    ticket_data = ticket_info
                    break
            if ticket_data:
                break
                
        if ticket_data:
            # Update last activity timestamp
            ticket_data["last_activity"] = discord.utils.utcnow()
            
            # Create an embed for the message
            embed = discord.Embed(
                description=message.content or "*No message content*",
                color=CONFIG["EMBED_COLOR"],
                timestamp=discord.utils.utcnow()
            )
            embed.set_author(name=message.author.name, icon_url=message.author.display_avatar.url)
            embed.set_footer(text=f"User ID: {message.author.id}")
            
            # Handle attachments
            files = []
            for attachment in message.attachments:
                try:
                    file_data = await attachment.read()
                    file = discord.File(io.BytesIO(file_data), filename=attachment.filename)
                    files.append(file)
                    
                    # Add attachment info to embed
                    embed.add_field(name="Attachment", value=f"[{attachment.filename}]({attachment.url})", inline=False)
                except Exception as e:
                    logger.error(f"Error processing attachment: {str(e)}")
            
            # Store the message in ticket data
            ticket_msg = await channel.send(embed=embed, files=files if files else None)
            ticket_data["messages"].append({
                "author_id": message.author.id,
                "content": message.content,
                "timestamp": discord.utils.utcnow().isoformat(),
                "attachments": [a.url for a in message.attachments],
                "channel_msg_id": ticket_msg.id,
                "user_msg_id": message.id
            })
            
            # Add reaction for marking as done
            await ticket_msg.add_reaction("✅")
            
            # Add reaction for quick reply
            await ticket_msg.add_reaction("↩️")
            
            # Add reaction for internal note
            await ticket_msg.add_reaction("📝")
            
            # Confirm receipt to user
            receipt = await message.add_reaction("✅")
            
            # Store attachment message ID for reaction handling
            if message.attachments:
                ticket_attachments[ticket_msg.id] = {
                    "user_id": message.author.id,
                    "channel_id": channel.id,
                    "attachments": [a.url for a in message.attachments]
                }
            
        else:
            # Ticket not found
            await message.channel.send("I couldn't find an active ticket to forward your message to. Please create a new ticket.")
            await send_welcome_message(message.author)
            
    except Exception as e:
        logger.error(f"Error forwarding message to channel: {str(e)}")
        await message.channel.send(f"Error sending your message: {str(e)}. Please try again later.")

@bot.event
async def on_reaction_add(reaction, user):
    """Handle reactions to messages"""
    # Ignore bot's own reactions
    if user.bot:
        return
        
    # Check if reaction is on a ticket channel message
    message = reaction.message
    if not isinstance(message.channel, discord.DMChannel):
        # Check if this is a ticket channel
        ticket_owner_id = None
        ticket_data = None
        
        for user_id, user_tickets in active_tickets.items():
            for channel_id, ticket_info in user_tickets.items():
                if channel_id == message.channel.id and not ticket_info["closed"]:
                    ticket_owner_id = user_id
                    ticket_data = ticket_info
                    break
            if ticket_owner_id:
                break
                
        if ticket_owner_id and ticket_data:
            # Handle quick reply reaction (↩️)
            if str(reaction.emoji) == "↩️":
                # Create modal for reply
                class ReplyModal(discord.ui.Modal):
                    def __init__(self):
                        super().__init__(title="Reply to User")
                        self.reply_content = discord.ui.TextInput(
                            label="Message",
                            style=discord.TextStyle.paragraph,
                            placeholder="Type your reply here...",
                            required=True
                        )
                        self.add_item(self.reply_content)
                        
                    async def on_submit(self, interaction: discord.Interaction):
                        # Get the ticket owner
                        ticket_user = await bot.fetch_user(ticket_owner_id)
                        
                        # Create embed for the reply
                        if CONFIG["ANONYMOUS_REPLIES"]:
                            author_name = "Staff"
                            author_avatar = bot.user.display_avatar.url
                        else:
                            author_name = user.name
                            author_avatar = user.display_avatar.url
                            
                        embed = discord.Embed(
                            description=self.reply_content.value,
                            color=CONFIG["EMBED_COLOR"],
                            timestamp=discord.utils.utcnow()
                        )
                        embed.set_author(name=author_name, icon_url=author_avatar)
                        embed.set_footer(text=f"Support System • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
                        
                        # Send the message to user
                        try:
                            user_message = await ticket_user.send(embed=embed)
                            
                            # Send confirmation
                            await interaction.response.send_message("Reply sent successfully!", ephemeral=True)
                            
                            # Store in ticket data
                            ticket_data["messages"].append({
                                "author_id": user.id,
                                "content": self.reply_content.value,
                                "timestamp": discord.utils.utcnow().isoformat(),
                                "attachments": []
                            })
                            
                            # Update last activity timestamp
                            ticket_data["last_activity"] = discord.utils.utcnow()
                            
                            # Show the reply in the channel too
                            staff_embed = discord.Embed(
                                description=self.reply_content.value,
                                color=CONFIG["SUPPORT_EMBED_COLOR"],
                                timestamp=discord.utils.utcnow()
                            )
                            staff_embed.set_author(name=f"{user.name} replied", icon_url=user.display_avatar.url)
                            await message.channel.send(embed=staff_embed)
                            
                        except Exception as e:
                            await interaction.response.send_message(f"Error sending reply: {str(e)}", ephemeral=True)
                
                # Start the modal
                await user.send_modal(ReplyModal())
                
            # Handle internal note reaction (📝)
            elif str(reaction.emoji) == "📝":
                # Create modal for note
                class NoteModal(discord.ui.Modal):
                    def __init__(self):
                        super().__init__(title="Internal Note")
                        self.note_content = discord.ui.TextInput(
                            label="Note",
                            style=discord.TextStyle.paragraph,
                            placeholder="Type your internal note here...",
                            required=True
                        )
                        self.add_item(self.note_content)
                        
                    async def on_submit(self, interaction: discord.Interaction):
                        # Create embed for the note
                        note_embed = discord.Embed(
                            title="Internal Note",
                            description=self.note_content.value,
                            color=0xFFA500,  # Orange color for notes
                            timestamp=discord.utils.utcnow()
                        )
                        note_embed.set_author(name=user.name, icon_url=user.display_avatar.url)
                        note_embed.set_footer(text="This note is only visible to staff")
                        
                        # Send the note to the channel
                        await message.channel.send(embed=note_embed)
                        await interaction.response.send_message("Internal note added!", ephemeral=True)
                
                # Start the modal
                await user.send_modal(NoteModal())

# Commands

@bot.command(name="ticket")
async def ticket_command(ctx, action="help", *, args=None):
    """Ticket management command"""
    # Check if command was used in a guild
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return
        
    # Check if user has staff roles
    if not any(role.name in CONFIG["STAFF_ROLES"] for role in ctx.author.roles):
        await ctx.send("You don't have permission to use this command.")
        return
        
    if action == "help":
        help_embed = discord.Embed(
            title="Ticket Command Help",
            description="Available ticket management commands:",
            color=CONFIG["EMBED_COLOR"]
        )
        help_embed.add_field(name="!ticket setup", value="Set up the ticket system in the current channel", inline=False)
        help_embed.add_field(name="!ticket blacklist @user", value="Blacklist a user from creating tickets", inline=False)
        help_embed.add_field(name="!ticket unblacklist @user", value="Remove a user from the blacklist", inline=False)
        help_embed.add_field(name="!ticket stats [user]", value="Show ticket statistics", inline=False)
        help_embed.add_field(name="!ticket config", value="Show current configuration", inline=False)
        await ctx.send(embed=help_embed)
        
    elif action == "setup":
        # Create ticket system embed with buttons
        setup_embed = discord.Embed(
            title="Support Ticket System",
            description="Click on one of the buttons below to create a new support ticket.",
            color=CONFIG["EMBED_COLOR"]
        )
        setup_embed.add_field(name="Support", value="General help and assistance", inline=True)
        setup_embed.add_field(name="Development", value="Technical issues and development requests", inline=True)
        setup_embed.add_field(name="Billing", value="Questions about payments and subscriptions", inline=True)
        setup_embed.add_field(name="Urgent", value="Critical issues requiring immediate attention", inline=True)
        setup_embed.set_footer(text=f"Support System • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Send the embed with buttons
        await ctx.send(embed=setup_embed, view=SupportTicketView())
        await ctx.send("Ticket system has been set up in this channel.", delete_after=5)
        
    elif action == "blacklist":
        # Blacklist a user
        if not ctx.message.mentions:
            await ctx.send("Please mention a user to blacklist.")
            return
            
        user = ctx.message.mentions[0]
        blacklisted_users.add(user.id)
        save_data()
        
        await ctx.send(f"{user.mention} has been blacklisted from creating tickets.")
        await log_action(ctx.guild, f"User blacklisted by {ctx.author.name} ({ctx.author.id})", "Blacklist", f"User: {user.name} ({user.id})")
        
    elif action == "unblacklist":
        # Remove a user from blacklist
        if not ctx.message.mentions:
            await ctx.send("Please mention a user to remove from the blacklist.")
            return
            
        user = ctx.message.mentions[0]
        if user.id in blacklisted_users:
            blacklisted_users.remove(user.id)
            save_data()
            
            await ctx.send(f"{user.mention} has been removed from the blacklist.")
            await log_action(ctx.guild, f"User unblacklisted by {ctx.author.name} ({ctx.author.id})", "Blacklist", f"User: {user.name} ({user.id})")
        else:
            await ctx.send(f"{user.mention} is not in the blacklist.")
            
    elif action == "stats":
        # Show ticket statistics
        stats_embed = discord.Embed(
            title="Ticket Statistics",
            color=CONFIG["EMBED_COLOR"],
            timestamp=discord.utils.utcnow()
        )
        
        # General stats
        active_count = sum(len(tickets) for tickets in active_tickets.values())
        total_users = len(active_tickets)
        blacklist_count = len(blacklisted_users)
        
        stats_embed.add_field(name="Active Tickets", value=str(active_count), inline=True)
        stats_embed.add_field(name="Users with Tickets", value=str(total_users), inline=True)
        stats_embed.add_field(name="Blacklisted Users", value=str(blacklist_count), inline=True)
        
        # User-specific stats if mentioned
        if ctx.message.mentions:
            user = ctx.message.mentions[0]
            user_active = len(active_tickets.get(user.id, {}))
            user_total = len(ticket_logs.get(str(user.id), []))
            
            stats_embed.add_field(name=f"{user.name}'s Stats", value=f"Active: {user_active}\nTotal: {user_total}", inline=False)
            
        await ctx.send(embed=stats_embed)
        
    elif action == "config":
        # Show current configuration
        config_embed = discord.Embed(
            title="Ticket System Configuration",
            color=CONFIG["EMBED_COLOR"],
            timestamp=discord.utils.utcnow()
        )
        
        for key, value in CONFIG.items():
            # Skip token for security
            if key == "TOKEN":
                continue
                
            if key == "STAFF_ROLES":
                config_embed.add_field(name=key, value=", ".join(value), inline=False)
            elif key.endswith("COLOR"):
                hex_color = f"#{value:06x}"
                config_embed.add_field(name=key, value=hex_color, inline=True)
            else:
                config_embed.add_field(name=key, value=str(value), inline=True)
                
        await ctx.send(embed=config_embed)

# Run the bot
bot.run(CONFIG["TOKEN"])
