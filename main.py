import asyncio
import os
import sys
import json
import logging
import time
import secrets
import re
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from aiohttp import web

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DiscordManager")

# Configuração Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ojjfwxjirlttpxcjhlho.supabase.co").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9qamZ3eGppcmx0dHB4Y2pobGhvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODkyMjExMjcsImV4cCI6MjEwNDc5NzEyN30.QiBcBHLwS2yWbmgi_oAKSmRU1UEFNRXgfyLujmEK7XU")
SUPABASE_TABLE = "discord_server_keys"

# Histórico de Ações para Reversão (Undo / Rollback)
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_history.json")
_server_history: dict[str, dict] = {}

def load_history():
    global _server_history
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                _server_history = json.load(f)
    except Exception as e:
        logger.warning(f"Erro ao carregar histórico: {e}")
        _server_history = {}

def save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_server_history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Erro ao salvar histórico: {e}")

def record_last_action(guild_id: int, action_data: dict):
    guild_key = str(guild_id)
    _server_history[guild_key] = action_data
    save_history()

def get_last_action(guild_id: int) -> dict | None:
    return _server_history.get(str(guild_id))

def clear_last_action(guild_id: int):
    guild_key = str(guild_id)
    if guild_key in _server_history:
        del _server_history[guild_key]
        save_history()

load_history()

# Caches de sincronização em memória
_sync_code_cache = {}        # guild_id_str -> sync_code
_code_to_server_cache = {}   # sync_code -> guild_id_int


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
def get_token():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        return token
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("bot_token")
        except Exception:
            pass
    return None

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.emojis = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =====================================================================
#  DETECÇÃO DE CARGOS COSMÉTICOS DE CORES (PALETAS / COLOR BOTS)
# =====================================================================
CSS_COLOR_NAMES = {
    # 140+ Cores Oficiais Web/CSS
    "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige", "bisque", "black",
    "blanchedalmond", "blue", "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse",
    "chocolate", "coral", "cornflowerblue", "cornsilk", "crimson", "crimsom", "cyan", "darkblue",
    "darkcyan", "darkgoldenrod", "darkgray", "darkgrey", "darkgreen", "darkkhaki", "darkmagenta",
    "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon", "darkseagreen",
    "darkslateblue", "darkslategray", "darkslategrey", "darkturquoise", "darkviolet", "deeppink",
    "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick", "floralwhite", "forestgreen",
    "fuchsia", "gainsboro", "ghostwhite", "gold", "goldenrod", "gray", "grey", "green", "greenyellow",
    "honeydew", "hotpink", "indianred", "indigo", "ivory", "khaki", "lavender", "lavenderblush",
    "lawngreen", "lemonchiffon", "lightblue", "lightcoral", "lightcyan", "lightgoldenrodyellow",
    "lightgray", "lightgrey", "lightgreen", "lightpink", "lightsalmon", "lightseagreen", "lightskyblue",
    "lightslategray", "lightslategrey", "lightsteelblue", "lightyellow", "lime", "limegreen", "linen",
    "magenta", "maroon", "mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple",
    "mediumseagreen", "mediumslateblue", "mediumspringgreen", "mediumturquoise", "mediumvioletred",
    "midnightblue", "mintcream", "mistyrose", "moccasin", "navajowhite", "navy", "oldlace", "olive",
    "olivedrab", "orange", "orangered", "orchid", "palegoldenrod", "palegreen", "paleturquoise",
    "palevioletred", "papayawhip", "peachpuff", "peru", "pink", "plum", "powderblue", "purple",
    "rebeccapurple", "red", "rosybrown", "royalblue", "saddlebrown", "salmon", "sandybrown",
    "seagreen", "seashell", "sienna", "silver", "skyblue", "slateblue", "slategray", "slategrey",
    "snow", "springgreen", "steelblue", "tan", "teal", "thistle", "tomato", "turquoise", "violet",
    "wheat", "white", "whitesmoke", "yellow", "yellowgreen",
    # Português
    "azul", "vermelho", "verde", "amarelo", "rosa", "roxo", "laranja", "marrom", "cinza",
    "preto", "branco", "ciano", "lilas", "lilás", "turquesa", "vinho", "bege", "dourado",
    "prata", "salmao", "salmão", "coral"
}

def is_color_role(role: discord.Role) -> bool:
    """Verifica se um cargo é puramente um cargo cosmético de cor (sem permissões operacionais)."""
    if not role or role.is_default() or role.managed:
        return False

    # Regra de Segurança: Nunca toca em cargos com qualquer permissão elevada/operacional
    elevated_perms = (
        discord.Permissions.administrator.flag
        | discord.Permissions.manage_guild.flag
        | discord.Permissions.manage_roles.flag
        | discord.Permissions.manage_channels.flag
        | discord.Permissions.kick_members.flag
        | discord.Permissions.ban_members.flag
        | discord.Permissions.manage_messages.flag
        | discord.Permissions.mention_everyone.flag
        | discord.Permissions.moderate_members.flag
        | discord.Permissions.manage_webhooks.flag
        | discord.Permissions.view_audit_log.flag
    )
    if (role.permissions.value & elevated_perms) != 0:
        return False

    # Deve ter uma cor personalizada atribuída (diferente de default #000000)
    if role.color.value == 0:
        return False

    cleaned_name = re.sub(r"[^a-zA-Z0-9áéíóúãõâêîôûàç]", "", role.name).lower()

    # 1. Nome do cargo bate com nomes de cores padrão
    if cleaned_name in CSS_COLOR_NAMES:
        return True

    # 2. Formato Hexadecimal de cor (ex: #FF5733 ou FF5733)
    if re.fullmatch(r"^[0-9a-f]{6}$", cleaned_name):
        return True

    # 3. Padrões comuns de bots de cor (ex: "cor-azul", "color red", "c-pink", "cor: verde")
    lower_raw = role.name.lower().strip()
    if lower_raw.startswith(("color-", "cor-", "color ", "cor ", "c-", "cor:", "color:")):
        return True

    return False


# =====================================================================
#  FUNÇÕES SEGURAS DE HIERARQUIA E SERVIDORES (SEM INDEXERROR)
# =====================================================================
def get_highest_role(guild: discord.Guild) -> discord.Role | None:
    """Retorna o cargo mais alto de forma 100% segura sem usar índices diretos."""
    if not guild or not guild.roles:
        return None
    valid_roles = [r for r in guild.roles if not r.is_default()]
    if not valid_roles:
        return getattr(guild, "default_role", None)
    return max(valid_roles, key=lambda r: r.position)


def check_bot_is_top(guild: discord.Guild) -> bool:
    """Verifica com resiliência se o cargo do bot está no topo da hierarquia."""
    if not guild:
        return False
    bot_member = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
    if not bot_member:
        return False
    highest_role = get_highest_role(guild)
    if not highest_role:
        return True
    return bot_member.top_role.position >= highest_role.position


def is_authorized_admin(interaction_or_ctx) -> bool:
    """Verifica se o usuário é o Dono do Servidor ou possui permissão de Administrador."""
    guild = getattr(interaction_or_ctx, "guild", None)
    if not guild:
        return False

    # Context tem .author, Interaction tem .user
    user = getattr(interaction_or_ctx, "author", None)
    if user is None:
        user = getattr(interaction_or_ctx, "user", None)

    if not user:
        return False

    # 1. O dono do servidor tem bypass absoluto
    if getattr(guild, "owner_id", None) and user.id == guild.owner_id:
        return True

    # 2. Dono da aplicação / bot
    if getattr(bot, "owner_id", None) and user.id == bot.owner_id:
        return True
    if getattr(bot, "owner_ids", None) and user.id in bot.owner_ids:
        return True

    # Se user não tiver guild_permissions diretamente, tenta obter o membro no servidor
    member = user
    if not hasattr(member, "guild_permissions") and hasattr(guild, "get_member"):
        m = guild.get_member(user.id)
        if m:
            member = m

    # 3. Permissões de administrador no membro
    guild_perms = getattr(member, "guild_permissions", None)
    if guild_perms and getattr(guild_perms, "administrator", False):
        return True

    # 4. Permissões na interação
    perms = getattr(interaction_or_ctx, "permissions", None)
    if isinstance(perms, discord.Permissions) and perms.administrator:
        return True

    return False


async def check_admin_permission(interaction: discord.Interaction) -> bool:
    """Bloqueio para membros não autorizados em Slash Commands."""
    if not is_authorized_admin(interaction):
        await interaction.response.send_message(
            "⛔ **Acesso Negado:** Apenas o Dono do Servidor ou Administradores autorizados podem executar comandos deste bot.",
            ephemeral=True
        )
        return False
    return True


# =====================================================================
#  BOTÕES DE CONFIRMAÇÃO COM SEGURANÇA (DISCORD UI)
# =====================================================================
class ConfirmDangerAction(discord.ui.View):
    def __init__(self, author_id: int, action_name: str):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.action_name = action_name
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Apenas quem disparou este comando pode confirmar.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Sim, Confirmar e Executar", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="❌ Operação cancelada pelo usuário.", view=None)


# =====================================================================
#  FUNÇÕES AUXILIARES DE CLONAGEM RESILIENTES
# =====================================================================
def build_overwrites(old_overwrites: dict, role_map: dict, target_guild: discord.Guild) -> dict:
    new_overwrites = {}
    for target, perms in old_overwrites.items():
        if isinstance(target, discord.Role):
            if target.is_default():
                new_overwrites[target_guild.default_role] = perms
            elif target in role_map:
                new_overwrites[role_map[target]] = perms
    return new_overwrites


async def execute_clone_roles(source_guild: discord.Guild, target_guild: discord.Guild, ignorar_cores: bool = False) -> tuple[dict, list[discord.Role]]:
    role_map = {source_guild.default_role: target_guild.default_role}
    try:
        await target_guild.default_role.edit(permissions=source_guild.default_role.permissions)
    except Exception:
        pass

    roles = [r for r in source_guild.roles if not r.is_default() and not r.managed]
    roles.sort(key=lambda r: r.position)
    existing_roles = {r.name: r for r in target_guild.roles if not r.managed}

    created_roles = []
    for role in roles:
        if ignorar_cores and is_color_role(role):
            continue
        try:
            if role.name in existing_roles:
                role_map[role] = existing_roles[role.name]
                continue

            new_role = await target_guild.create_role(
                name=role.name,
                permissions=role.permissions,
                color=role.color,
                hoist=role.hoist,
                mentionable=role.mentionable,
                reason=f"Clonado de {source_guild.name}"
            )
            role_map[role] = new_role
            created_roles.append(new_role)
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro ao criar cargo {role.name}: {e}")

    return role_map, created_roles


async def execute_clone_channels(source_guild: discord.Guild, target_guild: discord.Guild, role_map: dict = None) -> tuple[list[discord.CategoryChannel], list[discord.abc.GuildChannel]]:
    if role_map is None:
        role_map = {source_guild.default_role: target_guild.default_role}
        target_roles = {r.name: r for r in target_guild.roles}
        for r in source_guild.roles:
            if r.name in target_roles:
                role_map[r] = target_roles[r.name]

    cat_map = {}
    created_categories = []
    created_channels = []

    for cat in sorted(source_guild.categories, key=lambda c: c.position):
        try:
            overwrites = build_overwrites(cat.overwrites, role_map, target_guild)
            new_cat = await target_guild.create_category(name=cat.name, overwrites=overwrites)
            cat_map[cat.id] = new_cat
            created_categories.append(new_cat)
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro na categoria {cat.name}: {e}")

    for ch in sorted(source_guild.text_channels, key=lambda c: c.position):
        try:
            parent = cat_map.get(ch.category_id) if ch.category_id else None
            overwrites = build_overwrites(ch.overwrites, role_map, target_guild)
            new_ch = await target_guild.create_text_channel(
                name=ch.name,
                category=parent,
                topic=ch.topic,
                slowmode_delay=ch.slowmode_delay,
                nsfw=ch.nsfw,
                overwrites=overwrites
            )
            created_channels.append(new_ch)
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro no canal #{ch.name}: {e}")

    for vc in sorted(source_guild.voice_channels, key=lambda c: c.position):
        try:
            parent = cat_map.get(vc.category_id) if vc.category_id else None
            overwrites = build_overwrites(vc.overwrites, role_map, target_guild)
            bitrate = min(vc.bitrate, int(target_guild.bitrate_limit))
            new_vc = await target_guild.create_voice_channel(
                name=vc.name,
                category=parent,
                bitrate=bitrate,
                user_limit=vc.user_limit,
                overwrites=overwrites
            )
            created_channels.append(new_vc)
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro no canal de voz {vc.name}: {e}")

    return created_categories, created_channels


async def execute_clone_emojis(source_guild: discord.Guild, target_guild: discord.Guild) -> list[discord.Emoji]:
    existing = {e.name for e in target_guild.emojis}
    created_emojis = []
    for emoji in source_guild.emojis:
        if emoji.name in existing:
            continue
        try:
            img = await emoji.read()
            new_emoji = await target_guild.create_custom_emoji(name=emoji.name, image=img)
            created_emojis.append(new_emoji)
            await asyncio.sleep(0.5)
        except discord.HTTPException as e:
            if e.code == 30008:
                break
        except Exception:
            pass
    return created_emojis


async def execute_wipe_guild(guild: discord.Guild) -> tuple[list[dict], list[dict], discord.TextChannel | None]:
    """Limpa canais, categorias, cargos personalizados e emojis do servidor de forma segura."""
    # 1. Snapshot dos cargos e exclusão
    my_top_pos = guild.me.top_role.position if (guild.me and guild.me.top_role) else 999999
    roles_to_del = [r for r in guild.roles if not r.is_default() and not r.managed and getattr(r, "position", 0) < my_top_pos]
    snapshot_roles = [
        {
            "name": r.name,
            "permissions": r.permissions.value,
            "color": r.color.value,
            "hoist": r.hoist,
            "mentionable": r.mentionable
        }
        for r in roles_to_del
    ]
    for r in roles_to_del:
        try:
            await r.delete(reason="Limpeza prévia para clonagem total")
            await asyncio.sleep(0.2)
        except Exception:
            pass

    # 2. Snapshot dos canais, criação de canal temp e exclusão dos demais
    snapshot_channels = [
        {
            "name": ch.name,
            "type": "voice" if isinstance(ch, discord.VoiceChannel) else ("category" if isinstance(ch, discord.CategoryChannel) else "text"),
            "topic": getattr(ch, "topic", None)
        }
        for ch in guild.channels
    ]
    temp_ch = None
    try:
        temp_ch = await guild.create_text_channel(name="suporte-clonagem", reason="Canal temporário durante clonagem e limpeza")
    except Exception:
        pass

    for ch in list(guild.channels):
        if temp_ch and ch.id == temp_ch.id:
            continue
        try:
            await ch.delete(reason="Limpeza prévia para clonagem total")
            await asyncio.sleep(0.2)
        except Exception:
            pass

    # 3. Exclusão de emojis existentes
    for em in list(guild.emojis):
        try:
            await em.delete(reason="Limpeza prévia para clonagem total")
            await asyncio.sleep(0.2)
        except Exception:
            pass

    return snapshot_roles, snapshot_channels, temp_ch


async def execute_revert_action(guild: discord.Guild, action: dict) -> tuple[bool, str]:
    """Executa a reversão completa da ação com base no histórico."""
    act_type = action.get("type")

    # 1. Reverter clonagens (apagar o que foi criado e opcionalmente restaurar o que existia antes)
    if act_type in ("clonar_tudo", "clonar_cargos", "clonar_canais", "clonar_emojis"):
        del_emojis = 0
        del_channels = 0
        del_cats = 0
        del_roles = 0

        # Emojis criados
        for e_id in action.get("created_emoji_ids", []):
            emoji = guild.get_emoji(e_id)
            if emoji:
                try:
                    await emoji.delete(reason="Reversão de comando ServerManager")
                    del_emojis += 1
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

        # Canais criados
        for ch_id in action.get("created_channel_ids", []):
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.delete(reason="Reversão de comando ServerManager")
                    del_channels += 1
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

        # Categorias criadas
        for cat_id in action.get("created_category_ids", []):
            cat = guild.get_channel(cat_id)
            if cat:
                try:
                    await cat.delete(reason="Reversão de comando ServerManager")
                    del_cats += 1
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

        # Cargos criados
        for r_id in action.get("created_role_ids", []):
            role = guild.get_role(r_id)
            if role:
                try:
                    await role.delete(reason="Reversão de comando ServerManager")
                    del_roles += 1
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

        # Se limpou antes da clonagem, restaura os itens que existiam originalmente
        restored_roles = 0
        restored_chs = 0
        if action.get("limpou_antes"):
            for r_info in action.get("snapshot_roles", []):
                try:
                    perms = discord.Permissions(r_info.get("permissions", 0))
                    color = discord.Color(r_info.get("color", 0))
                    await guild.create_role(
                        name=r_info["name"],
                        permissions=perms,
                        color=color,
                        hoist=r_info.get("hoist", False),
                        mentionable=r_info.get("mentionable", False),
                        reason="Reversão de clonagem (restauração de cargos antigos)"
                    )
                    restored_roles += 1
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

            for ch_info in action.get("snapshot_channels", []):
                try:
                    ch_type = ch_info.get("type", "text")
                    if ch_type == "voice":
                        await guild.create_voice_channel(name=ch_info["name"], reason="Reversão de clonagem")
                    elif ch_type == "category":
                        await guild.create_category(name=ch_info["name"], reason="Reversão de clonagem")
                    else:
                        await guild.create_text_channel(name=ch_info["name"], topic=ch_info.get("topic"), reason="Reversão de clonagem")
                    restored_chs += 1
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

        report = []
        if del_roles > 0:
            report.append(f"• {del_roles} Cargos removidos")
        if del_cats > 0:
            report.append(f"• {del_cats} Categorias removidas")
        if del_channels > 0:
            report.append(f"• {del_channels} Canais removidos")
        if del_emojis > 0:
            report.append(f"• {del_emojis} Emojis removidos")
        if restored_roles > 0:
            report.append(f"• {restored_roles} Cargos anteriores restaurados")
        if restored_chs > 0:
            report.append(f"• {restored_chs} Canais anteriores restaurados")

        summary = "\n".join(report) if report else "Nenhum item alterado foi localizado."
        return True, f"⏪ **Reversão de Clonagem Concluída com Sucesso!**\n{summary}"

    # 2. Reverter reset de cargos (restaurar os cargos a partir do snapshot)
    elif act_type == "limpar_cargos":
        roles_data = action.get("snapshot_roles", [])
        restored = 0
        for r_info in roles_data:
            try:
                perms = discord.Permissions(r_info.get("permissions", 0))
                color = discord.Color(r_info.get("color", 0))
                await guild.create_role(
                    name=r_info["name"],
                    permissions=perms,
                    color=color,
                    hoist=r_info.get("hoist", False),
                    mentionable=r_info.get("mentionable", False),
                    reason="Reversão do reset de cargos"
                )
                restored += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"Erro ao restaurar cargo {r_info.get('name')}: {e}")
        return True, f"⏪ **Reversão Concluída!**\n• {restored} de {len(roles_data)} cargos foram restaurados com sucesso."

    # 3. Reverter exclusão de canais / categoria
    elif act_type in ("limpar_canais", "apagar_categoria"):
        channels_data = action.get("snapshot_channels") or action.get("snapshot_category", {}).get("channels", [])
        cat_name = action.get("snapshot_category", {}).get("name")
        restored_cat = 0
        restored_ch = 0

        target_cat = None
        if cat_name:
            try:
                target_cat = await guild.create_category(name=cat_name, reason="Reversão de exclusão de categoria")
                restored_cat += 1
            except Exception:
                pass

        for ch_info in channels_data:
            try:
                ch_type = ch_info.get("type", "voice" if ch_info.get("type") == "voice" else "text")
                parent = target_cat
                if ch_type == "voice":
                    await guild.create_voice_channel(name=ch_info["name"], category=parent, reason="Reversão de canais")
                else:
                    await guild.create_text_channel(name=ch_info["name"], category=parent, topic=ch_info.get("topic"), reason="Reversão de canais")
                restored_ch += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
        return True, f"⏪ **Reversão Concluída!**\n• {restored_ch} canais restaurados."

    return False, "Tipo de ação desconhecido para reversão."



async def get_owner_display(guild: discord.Guild) -> str:
    """Busca o dono do servidor de forma resiliente via cache ou API."""
    if not guild:
        return "Não identificado"

    owner = guild.owner
    if not owner and guild.owner_id:
        owner = guild.get_member(guild.owner_id)
    if not owner and guild.owner_id:
        try:
            owner = await bot.fetch_user(guild.owner_id)
        except Exception:
            pass
    if not owner:
        try:
            full_guild = await bot.fetch_guild(guild.id)
            if full_guild.owner_id:
                owner = await bot.fetch_user(full_guild.owner_id)
        except Exception:
            pass

    if owner:
        return f"**{owner.name}** (<@{owner.id}>)"
    if guild.owner_id:
        return f"<@{guild.owner_id}>"
    return "Não identificado"


def _generate_random_code() -> str:
    """Gera um código único e legível no formato SRV-XXXXXX"""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    random_part = "".join(secrets.choice(chars) for _ in range(6))
    return f"SRV-{random_part}"


async def get_or_create_sync_code(guild: discord.Guild) -> str:
    """Busca o código de sincronização no Supabase ou gera um novo e persiste."""
    if not guild:
        return "SRV-OFFLINE"

    guild_id_str = str(guild.id)
    if guild_id_str in _sync_code_cache:
        return _sync_code_cache[guild_id_str]

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

    # 1. Verifica se já existe no Supabase
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?server_id=eq.{guild_id_str}"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0 and data[0].get("sync_code"):
                        code = data[0]["sync_code"].upper()
                        _sync_code_cache[guild_id_str] = code
                        _code_to_server_cache[code] = guild.id
                        return code
    except Exception as e:
        logger.warning(f"Aviso ao consultar Supabase para guild {guild.id}: {e}")

    # 2. Gera novo código e salva no Supabase
    new_code = _generate_random_code()
    owner_id_str = str(guild.owner_id) if guild.owner_id else ""
    payload = {
        "server_id": guild_id_str,
        "sync_code": new_code,
        "server_name": guild.name,
        "owner_id": owner_id_str
    }

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status in (200, 201):
                    logger.info(f"Código {new_code} registrado no Supabase para servidor {guild.name}")
    except Exception as e:
        logger.warning(f"Aviso ao inserir código no Supabase: {e}")

    _sync_code_cache[guild_id_str] = new_code
    _code_to_server_cache[new_code] = guild.id
    return new_code


async def regenerate_sync_code(guild: discord.Guild) -> str:
    """Regenera um novo código aleatório e atualiza no Supabase."""
    new_code = _generate_random_code()
    guild_id_str = str(guild.id)
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?server_id=eq.{guild_id_str}"
            payload = {"sync_code": new_code, "server_name": guild.name}
            async with session.patch(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status in (200, 204):
                    logger.info(f"Código atualizado no Supabase para {new_code}")
    except Exception as e:
        logger.warning(f"Erro ao regenerar código no Supabase: {e}")

    _sync_code_cache[guild_id_str] = new_code
    _code_to_server_cache[new_code] = guild.id
    return new_code


async def get_server_id_from_sync_code(sync_code: str) -> int | None:
    """Busca o server_id correspondente ao código no Supabase ou cache."""
    if not sync_code:
        return None
    code_upper = sync_code.strip().upper()
    if code_upper in _code_to_server_cache:
        return _code_to_server_cache[code_upper]

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?sync_code=eq.{code_upper}"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0 and data[0].get("server_id"):
                        srv_id = int(data[0]["server_id"])
                        _code_to_server_cache[code_upper] = srv_id
                        _sync_code_cache[str(srv_id)] = code_upper
                        return srv_id
    except Exception as e:
        logger.warning(f"Erro ao buscar código {code_upper} no Supabase: {e}")

    return None


async def resolve_source_guild(input_str: str) -> tuple[discord.Guild | None, str | None]:
    """Resolve um servidor EXCLUSIVAMENTE pelo Código Privado do Supabase (ex: SRV-XXXXXX)."""
    if not input_str:
        return None, "Nenhum código fornecido."

    cleaned = input_str.strip().upper()

    # 1. Bloqueia tentativa de usar ID numérico público do Discord por segurança
    if input_str.strip().isdigit():
        return None, "🔒 **Acesso Negado:** Por segurança, não é permitido clonar servidores usando ID numérico público. Você só pode clonar se o dono do servidor de origem te passar o **Código Privado** dele (ex: `SRV-XXXXXX`)."

    # 2. Busca por código do Supabase
    code_to_check = cleaned if cleaned.startswith("SRV-") else f"SRV-{cleaned}"
    server_id = await get_server_id_from_sync_code(code_to_check)
    if not server_id:
        server_id = await get_server_id_from_sync_code(cleaned)

    if not server_id:
        return None, f"Código `{input_str}` não encontrado ou inválido. O responsável pelo servidor de origem precisa rodar `/setup` e te passar o código secreto."

    source_guild = bot.get_guild(int(server_id))
    if not source_guild:
        try:
            source_guild = await bot.fetch_guild(int(server_id))
        except Exception:
            pass

    if not source_guild:
        return None, f"Servidor associado ao código `{input_str}` encontrado, mas o bot não está nele! Adicione o bot ao servidor de origem primeiro."

    return source_guild, None


async def make_setup_embed(guild: discord.Guild) -> discord.Embed:
    """Gera o Embed de status do servidor de forma 100% segura contra erros e vazamentos."""
    bot_member = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
    is_top = check_bot_is_top(guild)
    is_admin = bot_member.guild_permissions.administrator if bot_member else False
    owner_str = await get_owner_display(guild)
    sync_code = await get_or_create_sync_code(guild)

    embed = discord.Embed(
        title="🛡️ ServerManager — Painel de Controle",
        description="Sistema de gerenciamento e clonagem de servidor ativo e 100% isolado.",
        color=0x335FFF
    )

    # Exibe o avatar oficial do bot no thumbnail e autor
    if bot.user and hasattr(bot.user, "display_avatar"):
        avatar_url = bot.user.display_avatar.url
        embed.set_thumbnail(url=avatar_url)
        embed.set_author(name="ServerManager", icon_url=avatar_url)

    embed.add_field(name="📍 Servidor Atual", value=f"**{guild.name}** (`{guild.id}`)", inline=False)
    embed.add_field(
        name="🔑 Código Privado de Clonagem",
        value=f"**`{sync_code}`**\n*(Mantenha em segredo! Apenas quem tiver este código poderá clonar este servidor.)*",
        inline=False
    )
    embed.add_field(name="👑 Dono do Servidor", value=owner_str, inline=True)
    embed.add_field(name="⚡ Permissão Admin", value="✅ Concedida" if is_admin else "❌ Ausente", inline=True)
    embed.add_field(name="📶 Posição no Topo", value="✅ No Topo dos Cargos" if is_top else "⚠️ Suba o cargo do bot para o topo!", inline=True)

    embed.add_field(
        name="📜 Comandos (Prefix ! ou Barra /)",
        value=(
            "• `/setup` ou `!setup` — Mostra este painel\n"
            "• `/reverter` ou `!reverter` — ⏪ Desfaz a última clonagem/ação\n"
            "• `/gerar_id` ou `!gerar_id` — Gera um novo código secreto\n"
            "• `!sync` — Força registro imediato dos comandos /\n"
            "• `/clonar_tudo <código>` — 🚀 Clona tudo (limpa antes por padrão)\n"
            "• `/clonar_cargos <código>` — Clona só cargos\n"
            "• `/clonar_canais <código>` — Clona só canais\n"
            "• `/clonar_emojis <código>` — Clona emojis\n"
            "• `/limpar_cores` ou `!limpar_cores` — 🎨 Apaga cargos cosméticos de cor\n"
            "• `/apagar_categoria <categoria>` — Apaga categoria inteira\n"
            "• `/limpar_canais` — Reseta todos os canais\n"
            "• `/limpar_cargos` — Reseta todos os cargos"
        ),
        inline=False
    )
    if guild.icon:
        embed.set_footer(text=f"Servidor: {guild.name} • Privacidade Ativa", icon_url=guild.icon.url)
    else:
        embed.set_footer(text="Privacidade Ativa: Nenhum outro servidor conectado é revelado.")
    return embed


# =====================================================================
#  SLASH COMMANDS (/)
# =====================================================================

@bot.tree.command(name="setup", description="Painel de controle e status do ServerManager.")
@app_commands.default_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return
    embed = await make_setup_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="gerar_id", description="Gera um novo código aleatório de clonagem para este servidor no Supabase.")
@app_commands.default_permissions(administrator=True)
async def cmd_gerar_id(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return
    new_code = await regenerate_sync_code(interaction.guild)
    await interaction.response.send_message(
        f"✅ **Novo Código Gerado e Salvo no Supabase!**\n"
        f"🔑 **`{new_code}`**\n"
        f"*(Use `/clonar_tudo {new_code}` no outro servidor para copiá-lo)*",
        ephemeral=True
    )


@bot.tree.command(name="clonar_tudo", description="Clona cargos, categorias, canais e emojis de outro servidor.")
@app_commands.describe(
    id_origem="Código (ex: SRV-XXXXXX) ou ID do servidor de onde você quer copiar",
    limpar_antes="Se True (padrão), limpa todos os canais e cargos existentes antes de clonar",
    ignorar_cores="Se True, não clona cargos cosméticos de cores (economiza limite de 250 cargos)"
)
@app_commands.default_permissions(administrator=True)
async def cmd_clonar_tudo(interaction: discord.Interaction, id_origem: str, limpar_antes: bool = True, ignorar_cores: bool = False):
    if not await check_admin_permission(interaction):
        return

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await interaction.response.send_message(f"❌ {err_msg}", ephemeral=True)

    if source_guild.id == interaction.guild_id:
        return await interaction.response.send_message("❌ O servidor de origem não pode ser o mesmo atual.", ephemeral=True)

    target_guild = interaction.guild
    view = ConfirmDangerAction(interaction.user.id, "Clonar Tudo")

    await interaction.response.send_message(
        f"⚠️ **Confirmar Clonagem Total**\n"
        f"Copiar de: **{source_guild.name}**\n"
        f"Para este servidor: **{target_guild.name}**\n"
        f"Limpeza prévia: {'**Sim (apagará canais e cargos existentes antes)**' if limpar_antes else '**Não**'}\n"
        f"Ignorar cargos de cores: {'**Sim**' if ignorar_cores else '**Não**'}\n\n"
        f"Isso criará uma estrutura idêntica à de **{source_guild.name}**.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        msg = await interaction.followup.send("🚀 Iniciando processo de clonagem...", ephemeral=True)

        snapshot_roles = []
        snapshot_channels = []
        temp_ch = None

        if limpar_antes:
            try:
                await msg.edit(content="🧹 [1/4] Limpando canais, categorias e cargos existentes...")
            except Exception:
                pass
            snapshot_roles, snapshot_channels, temp_ch = await execute_wipe_guild(target_guild)

        try:
            await msg.edit(content=f"{'🧹 Servidor limpo.\n' if limpar_antes else ''}🚀 [{'2/4' if limpar_antes else '1/3'}] Clonando cargos...")
        except Exception:
            pass

        role_map, created_roles = await execute_clone_roles(source_guild, target_guild, ignorar_cores=ignorar_cores)
        roles_cnt = len(created_roles)

        try:
            await msg.edit(content=f"✅ Cargos clonados ({roles_cnt}).\n🚀 [{'3/4' if limpar_antes else '2/3'}] Clonando categorias e canais...")
        except Exception:
            pass

        created_cats, created_chs = await execute_clone_channels(source_guild, target_guild, role_map)
        cats_cnt = len(created_cats)
        chs_cnt = len(created_chs)

        if temp_ch:
            try:
                await temp_ch.delete(reason="Removendo canal temporário após clonagem")
            except Exception:
                pass

        try:
            await msg.edit(content=f"✅ Cargos ({roles_cnt}) e Canais ({chs_cnt}) clonados.\n🚀 [{'4/4' if limpar_antes else '3/3'}] Clonando emojis...")
        except Exception:
            pass

        created_emojis = await execute_clone_emojis(source_guild, target_guild)
        emojis_cnt = len(created_emojis)

        record_last_action(target_guild.id, {
            "type": "clonar_tudo",
            "name": f"Clonagem Completa (de {source_guild.name})",
            "details": f"{roles_cnt} Cargos, {cats_cnt} Categorias, {chs_cnt} Canais, {emojis_cnt} Emojis{' (com limpeza prévia)' if limpar_antes else ''}",
            "created_role_ids": [r.id for r in created_roles],
            "created_category_ids": [c.id for c in created_cats],
            "created_channel_ids": [c.id for c in created_chs],
            "created_emoji_ids": [e.id for e in created_emojis],
            "limpou_antes": limpar_antes,
            "snapshot_roles": snapshot_roles,
            "snapshot_channels": snapshot_channels,
            "timestamp": time.time(),
            "author_id": interaction.user.id
        })

        success_text = (
            f"🎉 **Clonagem Completa Concluída com Sucesso!**\n"
            f"{'• 🧹 Servidor limpo previamente (canais e cargos anteriores removidos)\n' if limpar_antes else ''}"
            f"• 👥 **{roles_cnt}** Cargos clonados\n"
            f"• 📁 **{cats_cnt}** Categorias clonadas\n"
            f"• 💬 **{chs_cnt}** Canais clonados\n"
            f"• 😀 **{emojis_cnt}** Emojis clonados\n\n"
            f"💡 *Dica: Se precisar desfazer tudo, use `/reverter` ou `!reverter`.*"
        )

        try:
            await msg.edit(content=success_text)
        except Exception:
            pass

        notify_channel = None
        for ch in created_chs:
            if isinstance(ch, discord.TextChannel):
                notify_channel = ch
                break
        if notify_channel:
            try:
                await notify_channel.send(success_text)
            except Exception:
                pass


@bot.tree.command(name="clonar_cargos", description="Clona APENAS os cargos de outro servidor.")
@app_commands.describe(
    id_origem="Código (ex: SRV-XXXXXX) ou ID do servidor de origem",
    ignorar_cores="Se True, não clona cargos cosméticos de cores (economiza limite de 250 cargos)"
)
@app_commands.default_permissions(administrator=True)
async def cmd_clonar_cargos(interaction: discord.Interaction, id_origem: str, ignorar_cores: bool = False):
    if not await check_admin_permission(interaction):
        return

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await interaction.response.send_message(f"❌ {err_msg}", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    _, created_roles = await execute_clone_roles(source_guild, interaction.guild, ignorar_cores=ignorar_cores)
    roles_cnt = len(created_roles)

    record_last_action(interaction.guild.id, {
        "type": "clonar_cargos",
        "name": f"Clonagem de Cargos (de {source_guild.name})",
        "details": f"{roles_cnt} Cargos",
        "created_role_ids": [r.id for r in created_roles],
        "timestamp": time.time(),
        "author_id": interaction.user.id
    })

    await interaction.followup.send(f"✅ **Sucesso:** {roles_cnt} cargos clonados de **{source_guild.name}**!\n💡 *Use `/reverter` para desfazer se desejar.*", ephemeral=True)


@bot.tree.command(name="clonar_canais", description="Clona APENAS as categorias e canais de outro servidor.")
@app_commands.describe(id_origem="Código (ex: SRV-XXXXXX) ou ID do servidor de origem")
@app_commands.default_permissions(administrator=True)
async def cmd_clonar_canais(interaction: discord.Interaction, id_origem: str):
    if not await check_admin_permission(interaction):
        return

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await interaction.response.send_message(f"❌ {err_msg}", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    created_cats, created_chs = await execute_clone_channels(source_guild, interaction.guild)
    cats_cnt = len(created_cats)
    chs_cnt = len(created_chs)

    record_last_action(interaction.guild.id, {
        "type": "clonar_canais",
        "name": f"Clonagem de Canais (de {source_guild.name})",
        "details": f"{cats_cnt} Categorias, {chs_cnt} Canais",
        "created_category_ids": [c.id for c in created_cats],
        "created_channel_ids": [c.id for c in created_chs],
        "timestamp": time.time(),
        "author_id": interaction.user.id
    })

    await interaction.followup.send(f"✅ **Sucesso:** {cats_cnt} categorias e {chs_cnt} canais clonados de **{source_guild.name}**!\n💡 *Use `/reverter` para desfazer se desejar.*", ephemeral=True)


@bot.tree.command(name="clonar_emojis", description="Clona os emojis de outro servidor.")
@app_commands.describe(id_origem="Código (ex: SRV-XXXXXX) ou ID do servidor de origem")
@app_commands.default_permissions(administrator=True)
async def cmd_clonar_emojis(interaction: discord.Interaction, id_origem: str):
    if not await check_admin_permission(interaction):
        return

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await interaction.response.send_message(f"❌ {err_msg}", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    created_emojis = await execute_clone_emojis(source_guild, interaction.guild)
    count = len(created_emojis)

    record_last_action(interaction.guild.id, {
        "type": "clonar_emojis",
        "name": f"Clonagem de Emojis (de {source_guild.name})",
        "details": f"{count} Emojis",
        "created_emoji_ids": [e.id for e in created_emojis],
        "timestamp": time.time(),
        "author_id": interaction.user.id
    })

    await interaction.followup.send(f"✅ **Sucesso:** {count} emojis copiados de **{source_guild.name}**!\n💡 *Use `/reverter` para desfazer se desejar.*", ephemeral=True)


@bot.tree.command(name="apagar_categoria", description="Apaga uma categoria inteira e todos os canais contidos nela.")
@app_commands.describe(categoria="Selecione a categoria para apagar com todos os seus canais")
@app_commands.default_permissions(administrator=True)
async def cmd_apagar_categoria(interaction: discord.Interaction, categoria: discord.CategoryChannel):
    if not await check_admin_permission(interaction):
        return

    channels_count = len(categoria.channels)
    view = ConfirmDangerAction(interaction.user.id, "Apagar Categoria")

    await interaction.response.send_message(
        f"⚠️ **ATENÇÃO:** Isso irá apagar permanentemente a categoria **📁 {categoria.name}** e os seus **{channels_count} canais**!\n"
        f"Confirma a exclusão em massa?",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        snapshot_chs = [
            {"name": ch.name, "type": "voice" if isinstance(ch, discord.VoiceChannel) else "text", "topic": getattr(ch, "topic", None)}
            for ch in categoria.channels
        ]
        cat_name = categoria.name

        msg = await interaction.followup.send("🗑️ Apagando canais da categoria...", ephemeral=True)
        for ch in list(categoria.channels):
            try:
                await ch.delete(reason="Exclusão em massa solicitada via comando")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        try:
            await categoria.delete(reason="Exclusão em massa de categoria")
        except Exception:
            pass

        record_last_action(interaction.guild.id, {
            "type": "apagar_categoria",
            "name": f"Exclusão da Categoria {cat_name}",
            "details": f"Categoria '{cat_name}' e {len(snapshot_chs)} canais",
            "snapshot_category": {
                "name": cat_name,
                "channels": snapshot_chs
            },
            "timestamp": time.time(),
            "author_id": interaction.user.id
        })

        await msg.edit(content=f"✅ Categoria **{cat_name}** e todos os seus {channels_count} canais foram apagados!\n💡 *Use `/reverter` para restaurar se necessário.*")


@bot.tree.command(name="limpar_canais", description="🚨 Reseta o servidor: Apaga TODOS os canais existentes.")
@app_commands.default_permissions(administrator=True)
async def cmd_limpar_canais(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    guild = interaction.guild
    view = ConfirmDangerAction(interaction.user.id, "Limpar Todos os Canais")

    await interaction.response.send_message(
        f"🚨 **PERIGO MÁXIMO:** Você está prestes a apagar **TODOS OS {len(guild.channels)} CANAIS** de **{guild.name}**!\n"
        f"Esta ação pode ser desfeita usando `/reverter` logo em seguida.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        snapshot_chs = [
            {
                "name": ch.name,
                "type": "voice" if isinstance(ch, discord.VoiceChannel) else "text",
                "topic": getattr(ch, "topic", None)
            }
            for ch in guild.channels
        ]

        temp_ch = await guild.create_text_channel(name="suporte-reset", reason="Canal temporário durante reset")
        for ch in list(guild.channels):
            if ch.id == temp_ch.id:
                continue
            try:
                await ch.delete(reason="Reset geral de canais")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        record_last_action(guild.id, {
            "type": "limpar_canais",
            "name": "Reset Geral de Canais",
            "details": f"{len(snapshot_chs)} Canais excluídos",
            "snapshot_channels": snapshot_chs,
            "timestamp": time.time(),
            "author_id": interaction.user.id
        })

        await temp_ch.send("✅ **Reset de canais concluído!** Apenas este canal foi mantido para você continuar.\n💡 *Use `/reverter` caso deseje restaurar os canais antigos.*")


@bot.tree.command(name="limpar_cargos", description="🚨 Apaga todos os cargos personalizados do servidor.")
@app_commands.default_permissions(administrator=True)
async def cmd_limpar_cargos(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    guild = interaction.guild
    roles = [r for r in guild.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
    view = ConfirmDangerAction(interaction.user.id, "Limpar Todos os Cargos")

    await interaction.response.send_message(
        f"⚠️ **Confirmar Reset de Cargos:** {len(roles)} cargos personalizados serão excluídos em **{guild.name}**.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        snapshot_roles = [
            {
                "name": r.name,
                "permissions": r.permissions.value,
                "color": r.color.value,
                "hoist": r.hoist,
                "mentionable": r.mentionable
            }
            for r in roles
        ]

        deleted = 0
        for r in roles:
            try:
                await r.delete(reason="Reset de cargos solicitado por administrador")
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass

        record_last_action(guild.id, {
            "type": "limpar_cargos",
            "name": "Reset de Cargos",
            "details": f"{deleted} Cargos excluídos",
            "snapshot_roles": snapshot_roles,
            "timestamp": time.time(),
            "author_id": interaction.user.id
        })

        await interaction.followup.send(f"✅ Reset concluído! {deleted} cargos foram excluídos.\n💡 *Use `/reverter` para restaurá-los se desejar.*", ephemeral=True)


@bot.tree.command(name="limpar_cores", description="🎨 Apaga apenas os cargos cosméticos de cores para liberar limite de 250 cargos.")
@app_commands.default_permissions(administrator=True)
async def cmd_limpar_cores(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    guild = interaction.guild
    color_roles = [r for r in guild.roles if is_color_role(r) and r < guild.me.top_role]

    if not color_roles:
        return await interaction.response.send_message(
            "ℹ️ Nenhum cargo cosmético de cor foi encontrado neste servidor para remoção.",
            ephemeral=True
        )

    view = ConfirmDangerAction(interaction.user.id, "Limpar Cargos de Cores")
    preview_names = ", ".join([f"`{r.name}`" for r in color_roles[:8]])
    if len(color_roles) > 8:
        preview_names += f" e mais {len(color_roles) - 8}..."

    await interaction.response.send_message(
        f"🎨 **Confirmar Remoção de Cargos de Cores**\n"
        f"Foram encontrados **{len(color_roles)} cargos cosméticos de cor** em **{guild.name}**.\n"
        f"Exemplos identificados: {preview_names}\n\n"
        f"*(Cargos com permissões de staff/moderação são 100% preservados e nunca apagados)*\n"
        f"Deseja apagar todos esses cargos de cores para liberar espaço?",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        snapshot_roles = [
            {
                "name": r.name,
                "permissions": r.permissions.value,
                "color": r.color.value,
                "hoist": r.hoist,
                "mentionable": r.mentionable
            }
            for r in color_roles
        ]

        deleted = 0
        for r in color_roles:
            try:
                await r.delete(reason="Limpeza de cargos cosméticos de cor solicitada por administrador")
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass

        record_last_action(guild.id, {
            "type": "limpar_cargos",
            "name": "Limpeza de Cargos de Cores",
            "details": f"{deleted} Cargos de cor removidos",
            "snapshot_roles": snapshot_roles,
            "timestamp": time.time(),
            "author_id": interaction.user.id
        })

        await interaction.followup.send(
            f"✅ **Limpeza Concluída!** {deleted} cargos cosméticos de cor foram excluídos com sucesso, liberando espaço no limite de 250 cargos.\n💡 *Use `/reverter` para restaurá-los se desejar.*",
            ephemeral=True
        )


@bot.tree.command(name="reverter", description="Reverte a última ação executada no servidor (ex: desfaz clonagens ou exclusões).")
@app_commands.default_permissions(administrator=True)
async def cmd_reverter(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    action = get_last_action(interaction.guild_id)
    if not action:
        return await interaction.response.send_message(
            "ℹ️ **Nenhuma ação recente registrada para reverter neste servidor.**\n"
            "O histórico registra comandos executados como `/clonar_tudo`, `/clonar_cargos`, `/clonar_canais`, `/clonar_emojis`, etc.",
            ephemeral=True
        )

    elapsed = int(time.time() - action.get("timestamp", time.time()))
    mins = elapsed // 60
    time_str = f"há {mins} minuto(s)" if mins > 0 else "há poucos segundos"
    author_str = f"<@{action.get('author_id')}>" if action.get("author_id") else "Administrador"

    view = ConfirmDangerAction(interaction.user.id, "Reverter Ação")
    await interaction.response.send_message(
        f"⏪ **Reverter Última Ação do Servidor**\n\n"
        f"• **Ação:** {action.get('name', 'Comando')}\n"
        f"• **Detalhes:** {action.get('details', 'N/A')}\n"
        f"• **Executado por:** {author_str} ({time_str})\n\n"
        f"⚠️ **Confirmação:** Deseja desfazer e reverter esta ação agora?",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        msg = await interaction.followup.send("⏳ Processando reversão do comando...", ephemeral=True)
        success, result_text = await execute_revert_action(interaction.guild, action)
        if success:
            clear_last_action(interaction.guild_id)
        await msg.edit(content=result_text)



# =====================================================================
#  PREFIX COMMANDS (!) — FUNCIONAM IMEDIATAMENTE SEM ESPERAR DISCORD
# =====================================================================

@bot.command(name="setup")
async def prefix_setup(ctx: commands.Context):
    """Comando !setup com checagem segura contra IndexError."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado. Apenas o Dono ou Administradores podem usar.")
    embed = await make_setup_embed(ctx.guild)
    await ctx.reply(embed=embed)


@bot.command(name="gerar_id")
async def prefix_gerar_id(ctx: commands.Context):
    """Gera um novo código aleatório de clonagem para o servidor no Supabase."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    new_code = await regenerate_sync_code(ctx.guild)
    await ctx.reply(
        f"✅ **Novo Código Gerado e Salvo no Supabase!**\n"
        f"🔑 **`{new_code}`**\n"
        f"*(Use `!clonar_tudo {new_code}` no outro servidor para copiá-lo)*"
    )


@bot.command(name="sync")
async def prefix_sync(ctx: commands.Context):
    """Remove comandos slash duplicados e sincroniza a árvore oficial."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")

    msg = await ctx.reply("⏳ Removendo comandos duplicados e sincronizando...")
    try:
        # 1. Limpa comandos registrados no nível da guilda (origem das duplicatas)
        bot.tree.clear_commands(guild=ctx.guild)
        await bot.tree.sync(guild=ctx.guild)

        # 2. Sincroniza a árvore global única
        synced = await bot.tree.sync()
        await msg.edit(content=f"✅ Sincronização concluída! Comandos locais duplicados foram removidos ({len(synced)} comandos únicos ativos).\n💡 *Pressione `Ctrl + R` no Discord para atualizar o cache.*")
    except Exception as e:
        await msg.edit(content=f"❌ Erro ao sincronizar: {e}")


@bot.command(name="clonar_tudo")
async def prefix_clonar_tudo(ctx: commands.Context, id_origem: str = None, limpar_antes: str = "sim", ignorar_cores: str = "nao"):
    """Clona tudo via comando de prefixo !clonar_tudo <id ou código> [limpar_antes=sim] [ignorar_cores=nao]."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    if not id_origem:
        return await ctx.reply("❌ Use: `!clonar_tudo <código ou id_do_servidor_origem> [limpar: sim/nao] [ignorar_cores: sim/nao]`")

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await ctx.reply(f"❌ {err_msg}")

    limpar = limpar_antes.lower() in ("true", "1", "sim", "yes", "s", "limpar")
    ignorar = ignorar_cores.lower() in ("true", "1", "sim", "yes", "s", "ignorar")

    target_guild = ctx.guild
    msg = await ctx.reply(f"🚀 Iniciando clonagem de **{source_guild.name}** (limpar tudo antes: {'Sim' if limpar else 'Não'}, ignorar cores: {'Sim' if ignorar else 'Não'})...")

    snapshot_roles = []
    snapshot_channels = []
    temp_ch = None

    if limpar:
        try:
            await msg.edit(content="🧹 [1/4] Limpando canais, categorias e cargos existentes...")
        except Exception:
            pass
        snapshot_roles, snapshot_channels, temp_ch = await execute_wipe_guild(target_guild)

    try:
        await msg.edit(content=f"{'🧹 Servidor limpo.\n' if limpar else ''}🚀 [{'2/4' if limpar else '1/3'}] Clonando cargos...")
    except Exception:
        pass

    role_map, created_roles = await execute_clone_roles(source_guild, target_guild, ignorar_cores=ignorar)
    roles_cnt = len(created_roles)

    try:
        await msg.edit(content=f"✅ Cargos clonados ({roles_cnt}).\n🚀 [{'3/4' if limpar else '2/3'}] Clonando categorias e canais...")
    except Exception:
        pass

    created_cats, created_chs = await execute_clone_channels(source_guild, target_guild, role_map)
    cats_cnt = len(created_cats)
    chs_cnt = len(created_chs)

    if temp_ch:
        try:
            await temp_ch.delete(reason="Removendo canal temporário após clonagem")
        except Exception:
            pass

    try:
        await msg.edit(content=f"✅ Cargos ({roles_cnt}) e Canais ({chs_cnt}) clonados.\n🚀 [{'4/4' if limpar else '3/3'}] Clonando emojis...")
    except Exception:
        pass

    created_emojis = await execute_clone_emojis(source_guild, target_guild)
    emojis_cnt = len(created_emojis)

    record_last_action(target_guild.id, {
        "type": "clonar_tudo",
        "name": f"Clonagem Completa (de {source_guild.name})",
        "details": f"{roles_cnt} Cargos, {cats_cnt} Categorias, {chs_cnt} Canais, {emojis_cnt} Emojis{' (com limpeza prévia)' if limpar else ''}",
        "created_role_ids": [r.id for r in created_roles],
        "created_category_ids": [c.id for c in created_cats],
        "created_channel_ids": [c.id for c in created_chs],
        "created_emoji_ids": [e.id for e in created_emojis],
        "limpou_antes": limpar,
        "snapshot_roles": snapshot_roles,
        "snapshot_channels": snapshot_channels,
        "timestamp": time.time(),
        "author_id": ctx.author.id
    })

    success_text = (
        f"🎉 **Clonagem Concluída com Sucesso!**\n"
        f"{'• 🧹 Servidor limpo previamente (canais e cargos anteriores removidos)\n' if limpar else ''}"
        f"• 👥 **{roles_cnt}** Cargos\n"
        f"• 📁 **{cats_cnt}** Categorias\n"
        f"• 💬 **{chs_cnt}** Canais\n"
        f"• 😀 **{emojis_cnt}** Emojis\n\n"
        f"💡 *Dica: Se precisar desfazer, use `!reverter` ou `/reverter`.*"
    )

    try:
        await msg.edit(content=success_text)
    except Exception:
        pass

    notify_channel = None
    for ch in created_chs:
        if isinstance(ch, discord.TextChannel):
            notify_channel = ch
            break
    if notify_channel:
        try:
            await notify_channel.send(success_text)
        except Exception:
            pass


@bot.command(name="clonar_cargos")
async def prefix_clonar_cargos(ctx: commands.Context, id_origem: str = None, ignorar_cores: str = "nao"):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    if not id_origem:
        return await ctx.reply("❌ Use: `!clonar_cargos <código ou id_do_servidor_origem> [sim/nao]`")

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await ctx.reply(f"❌ {err_msg}")

    ignorar = ignorar_cores.lower() in ("true", "1", "sim", "yes", "s", "ignorar")
    msg = await ctx.reply(f"🚀 Clonando cargos de **{source_guild.name}** (ignorar cores: {'Sim' if ignorar else 'Não'})...")
    _, created_roles = await execute_clone_roles(source_guild, ctx.guild, ignorar_cores=ignorar)
    roles_cnt = len(created_roles)

    record_last_action(ctx.guild.id, {
        "type": "clonar_cargos",
        "name": f"Clonagem de Cargos (de {source_guild.name})",
        "details": f"{roles_cnt} Cargos",
        "created_role_ids": [r.id for r in created_roles],
        "timestamp": time.time(),
        "author_id": ctx.author.id
    })

    await msg.edit(content=f"✅ Sucesso! {roles_cnt} cargos clonados de **{source_guild.name}**.\n💡 *Use `!reverter` para desfazer se desejar.*")


@bot.command(name="clonar_canais")
async def prefix_clonar_canais(ctx: commands.Context, id_origem: str = None):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    if not id_origem:
        return await ctx.reply("❌ Use: `!clonar_canais <código ou id_do_servidor_origem>`")

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await ctx.reply(f"❌ {err_msg}")

    msg = await ctx.reply(f"🚀 Clonando canais de **{source_guild.name}**...")
    created_cats, created_chs = await execute_clone_channels(source_guild, ctx.guild)
    cats_cnt = len(created_cats)
    chs_cnt = len(created_chs)

    record_last_action(ctx.guild.id, {
        "type": "clonar_canais",
        "name": f"Clonagem de Canais (de {source_guild.name})",
        "details": f"{cats_cnt} Categorias, {chs_cnt} Canais",
        "created_category_ids": [c.id for c in created_cats],
        "created_channel_ids": [c.id for c in created_chs],
        "timestamp": time.time(),
        "author_id": ctx.author.id
    })

    await msg.edit(content=f"✅ Sucesso! {cats_cnt} categorias e {chs_cnt} canais clonados.\n💡 *Use `!reverter` para desfazer se desejar.*")


@bot.command(name="clonar_emojis")
async def prefix_clonar_emojis(ctx: commands.Context, id_origem: str = None):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    if not id_origem:
        return await ctx.reply("❌ Use: `!clonar_emojis <código ou id_do_servidor_origem>`")

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await ctx.reply(f"❌ {err_msg}")

    msg = await ctx.reply(f"🚀 Clonando emojis de **{source_guild.name}**...")
    created_emojis = await execute_clone_emojis(source_guild, ctx.guild)
    count = len(created_emojis)

    record_last_action(ctx.guild.id, {
        "type": "clonar_emojis",
        "name": f"Clonagem de Emojis (de {source_guild.name})",
        "details": f"{count} Emojis",
        "created_emoji_ids": [e.id for e in created_emojis],
        "timestamp": time.time(),
        "author_id": ctx.author.id
    })

    await msg.edit(content=f"✅ Sucesso! {count} emojis copiados.\n💡 *Use `!reverter` para desfazer se desejar.*")


@bot.command(name="apagar_categoria")
async def prefix_apagar_categoria(ctx: commands.Context, *, nome_ou_id: str = None):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    if not nome_ou_id:
        return await ctx.reply("❌ Use: `!apagar_categoria <nome ou ID da categoria>`")

    cat_target = None
    for cat in ctx.guild.categories:
        if str(cat.id) == nome_ou_id.strip() or cat.name.lower() == nome_ou_id.strip().lower():
            cat_target = cat
            break

    if not cat_target:
        return await ctx.reply("❌ Categoria não encontrada.")

    channels_count = len(cat_target.channels)
    snapshot_chs = [
        {"name": ch.name, "type": "voice" if isinstance(ch, discord.VoiceChannel) else "text", "topic": getattr(ch, "topic", None)}
        for ch in cat_target.channels
    ]
    cat_name = cat_target.name

    msg = await ctx.reply(f"🗑️ Apagando categoria '{cat_target.name}' e seus {channels_count} canais...")
    for ch in list(cat_target.channels):
        try:
            await ch.delete(reason="Exclusão em massa solicitada via !apagar_categoria")
            await asyncio.sleep(0.3)
        except Exception:
            pass

    try:
        await cat_target.delete(reason="Exclusão em massa de categoria")
    except Exception:
        pass

    record_last_action(ctx.guild.id, {
        "type": "apagar_categoria",
        "name": f"Exclusão da Categoria {cat_name}",
        "details": f"Categoria '{cat_name}' e {len(snapshot_chs)} canais",
        "snapshot_category": {
            "name": cat_name,
            "channels": snapshot_chs
        },
        "timestamp": time.time(),
        "author_id": ctx.author.id
    })

    await msg.edit(content=f"✅ Categoria **{cat_name}** e todos os seus {channels_count} canais foram apagados!\n💡 *Use `!reverter` para restaurá-los se necessário.*")


@bot.command(name="limpar_canais")
async def prefix_limpar_canais(ctx: commands.Context):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")

    view = ConfirmDangerAction(ctx.author.id, "Limpar Canais")
    confirm_msg = await ctx.reply("⚠️ **ATENÇÃO:** Isso apagará TODOS os canais deste servidor! Confirma?", view=view)
    await view.wait()

    if view.value:
        snapshot_chs = [
            {
                "name": ch.name,
                "type": "voice" if isinstance(ch, discord.VoiceChannel) else "text",
                "topic": getattr(ch, "topic", None)
            }
            for ch in ctx.guild.channels
        ]

        temp_ch = await ctx.guild.create_text_channel(name="suporte-reset", reason="Canal temporário durante reset")
        for ch in list(ctx.guild.channels):
            if ch.id == temp_ch.id:
                continue
            try:
                await ch.delete(reason="Reset geral de canais")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        record_last_action(ctx.guild.id, {
            "type": "limpar_canais",
            "name": "Reset Geral de Canais",
            "details": f"{len(snapshot_chs)} Canais excluídos",
            "snapshot_channels": snapshot_chs,
            "timestamp": time.time(),
            "author_id": ctx.author.id
        })

        await temp_ch.send("✅ **Reset de canais concluído!** Apenas este canal foi mantido para você continuar.\n💡 *Use `!reverter` caso deseje restaurar os canais antigos.*")


@bot.command(name="limpar_cargos")
async def prefix_limpar_cargos(ctx: commands.Context):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")

    roles = [r for r in ctx.guild.roles if not r.is_default() and not r.managed and r < ctx.guild.me.top_role]
    view = ConfirmDangerAction(ctx.author.id, "Limpar Cargos")
    confirm_msg = await ctx.reply(f"⚠️ **ATENÇÃO:** Isso apagará {len(roles)} cargos personalizados! Confirma?", view=view)
    await view.wait()

    if view.value:
        snapshot_roles = [
            {
                "name": r.name,
                "permissions": r.permissions.value,
                "color": r.color.value,
                "hoist": r.hoist,
                "mentionable": r.mentionable
            }
            for r in roles
        ]

        deleted = 0
        for r in roles:
            try:
                await r.delete(reason="Reset de cargos solicitado por administrador")
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass

        record_last_action(ctx.guild.id, {
            "type": "limpar_cargos",
            "name": "Reset de Cargos",
            "details": f"{deleted} Cargos excluídos",
            "snapshot_roles": snapshot_roles,
            "timestamp": time.time(),
            "author_id": ctx.author.id
        })

        await ctx.reply(f"✅ Reset concluído! {deleted} cargos foram excluídos.\n💡 *Use `!reverter` para restaurá-los se desejar.*")


@bot.command(name="limpar_cores")
async def prefix_limpar_cores(ctx: commands.Context):
    """Apaga apenas cargos cosméticos de cor via !limpar_cores."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")

    color_roles = [r for r in ctx.guild.roles if is_color_role(r) and r < ctx.guild.me.top_role]
    if not color_roles:
        return await ctx.reply("ℹ️ Nenhum cargo cosmético de cor foi encontrado neste servidor para remoção.")

    view = ConfirmDangerAction(ctx.author.id, "Limpar Cargos de Cores")
    preview_names = ", ".join([f"`{r.name}`" for r in color_roles[:8]])
    if len(color_roles) > 8:
        preview_names += f" e mais {len(color_roles) - 8}..."

    confirm_msg = await ctx.reply(
        f"🎨 **Confirmar Remoção de Cargos de Cores**\n"
        f"Foram encontrados **{len(color_roles)} cargos cosméticos de cor** em **{ctx.guild.name}**.\n"
        f"Exemplos identificados: {preview_names}\n\n"
        f"*(Cargos com permissões de staff/moderação são 100% preservados)*\n"
        f"Deseja apagar todos esses cargos de cores para liberar espaço?",
        view=view
    )
    await view.wait()

    if view.value:
        snapshot_roles = [
            {
                "name": r.name,
                "permissions": r.permissions.value,
                "color": r.color.value,
                "hoist": r.hoist,
                "mentionable": r.mentionable
            }
            for r in color_roles
        ]

        deleted = 0
        for r in color_roles:
            try:
                await r.delete(reason="Limpeza de cargos cosméticos de cor via !limpar_cores")
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass

        record_last_action(ctx.guild.id, {
            "type": "limpar_cargos",
            "name": "Limpeza de Cargos de Cores",
            "details": f"{deleted} Cargos de cor removidos",
            "snapshot_roles": snapshot_roles,
            "timestamp": time.time(),
            "author_id": ctx.author.id
        })

        await ctx.reply(f"✅ **Limpeza Concluída!** {deleted} cargos de cores excluídos com sucesso.\n💡 *Use `!reverter` para restaurá-los se desejar.*")


@bot.command(name="reverter")
async def prefix_reverter(ctx: commands.Context):
    """Reverte a última ação executada no servidor via !reverter."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado. Apenas o Dono ou Administradores podem usar.")

    action = get_last_action(ctx.guild.id)
    if not action:
        return await ctx.reply(
            "ℹ️ **Nenhuma ação recente registrada para reverter neste servidor.**\n"
            "O histórico registra comandos executados como `!clonar_tudo`, `!clonar_cargos`, etc."
        )

    elapsed = int(time.time() - action.get("timestamp", time.time()))
    mins = elapsed // 60
    time_str = f"há {mins} minuto(s)" if mins > 0 else "há poucos segundos"
    author_str = f"<@{action.get('author_id')}>" if action.get("author_id") else "Administrador"

    view = ConfirmDangerAction(ctx.author.id, "Reverter Ação")
    confirm_msg = await ctx.reply(
        f"⏪ **Reverter Última Ação do Servidor**\n\n"
        f"• **Ação:** {action.get('name', 'Comando')}\n"
        f"• **Detalhes:** {action.get('details', 'N/A')}\n"
        f"• **Executado por:** {author_str} ({time_str})\n\n"
        f"⚠️ **Confirmação:** Deseja desfazer e reverter esta ação agora?",
        view=view
    )
    await view.wait()

    if view.value:
        progress_msg = await ctx.reply("⏳ Processando reversão do comando...")
        success, result_text = await execute_revert_action(ctx.guild, action)
        if success:
            clear_last_action(ctx.guild.id)
        await progress_msg.edit(content=result_text)



# =====================================================================
#  TRATAMENTO GLOBAL DE ERROS (SLASH E PREFIXO)
# =====================================================================
@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f"Erro em slash command: {error}")
    err_text = f"⚠️ Ocorreu um erro ao executar este comando: `{error}`"
    if interaction.response.is_done():
        await interaction.followup.send(err_text, ephemeral=True)
    else:
        await interaction.response.send_message(err_text, ephemeral=True)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    logger.error(f"Erro em prefix command: {error}")
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"❌ Argumento ausente: `{error.param.name}`. Digite `!setup` para ver os exemplos.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.reply(f"⚠️ Erro ao executar: `{error}`")


# =====================================================================
#  SERVIDOR WEB DE HEALTH CHECK (Para manter ativo no Render/Railway)
# =====================================================================
async def handle_healthcheck(request):
    status_data = {
        "status": "online",
        "bot": bot.user.name if bot.user else "connecting",
        "servers": len(bot.guilds),
        "latency_ms": round(bot.latency * 1000, 2)
    }
    return web.json_response(status_data)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_healthcheck)
    app.router.add_get("/health", handle_healthcheck)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Servidor Web de Health Check ativo na porta {port}")


# =====================================================================
#  EVENTOS DO BOT
# =====================================================================
@bot.event
async def on_ready():
    logger.info(f"Bot conectado como {bot.user} (ID: {bot.user.id})")

    # Limpa comandos locais de cada servidor para eliminar qualquer duplicata
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            logger.info(f"Comandos locais limpos com sucesso no servidor: {guild.name}")
        except Exception as e:
            logger.warning(f"Erro ao limpar comandos locais na guild {guild.name}: {e}")

        # Tenta exibir o cargo do bot destacado no tab (hoist=True)
        try:
            bot_member = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
            if bot_member and bot_member.guild_permissions.manage_roles:
                top_r = bot_member.top_role
                if top_r and not top_r.is_default() and not top_r.hoist:
                    await top_r.edit(hoist=True, reason="Destacar ServerManager no tab de membros")
        except Exception:
            pass

    # Sincroniza APENAS a árvore global única (elimina 100% dos comandos duplicados)
    try:
        synced = await bot.tree.sync()
        logger.info(f"{len(synced)} comandos slash globais sincronizados com sucesso.")
    except Exception as e:
        logger.warning(f"Erro ao sincronizar comandos globais: {e}")

    try:
        await start_web_server()
    except Exception as e:
        logger.warning(f"Web server warning: {e}")

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="seus servidores | /setup | !setup"),
        status=discord.Status.online
    )


@bot.event
async def on_guild_join(guild: discord.Guild):
    logger.info(f"Bot adicionado ao servidor: {guild.name} (ID: {guild.id})")

    # 1. Tenta destacar o bot imediatamente no tab de membros (hoist=True)
    try:
        bot_member = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
        if bot_member and bot_member.guild_permissions.manage_roles:
            top_r = bot_member.top_role
            if top_r and not top_r.is_default() and not top_r.hoist:
                await top_r.edit(hoist=True, reason="Destacar ServerManager no tab de membros")
    except Exception as e:
        logger.debug(f"Ajuste hoist on_guild_join: {e}")

    # 2. Garante que não haja comandos locais duplicados no novo servidor
    try:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info(f"Comandos locais limpos no novo servidor: {guild.name}")
    except Exception as e:
        logger.warning(f"Erro ao limpar comandos locais no novo servidor: {e}")

    # 3. Envia confirmação visual imediata com embed no canal principal
    try:
        target_ch = guild.system_channel
        if not target_ch or not target_ch.permissions_for(guild.me).send_messages:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target_ch = ch
                    break

        if target_ch:
            embed = await make_setup_embed(guild)
            await target_ch.send(
                "👋 **ServerManager Conectado e Pronto!**\n"
                "⚡ **Dica de Performance:** Se os comandos `/` demorarem alguns segundos para aparecer no seu cliente Discord devido ao cache local, use o prefixo imediato `!setup` ou aperte `Ctrl + R` no Discord.",
                embed=embed
            )
    except Exception as e:
        logger.debug(f"Mensagem de boas-vindas on_guild_join: {e}")


def run():
    token = get_token()
    if not token:
        print("[X] Token não encontrado no ambiente nem no config.json.")
        sys.exit(1)
    bot.run(token)


if __name__ == "__main__":
    run()
