import asyncio
import os
import sys
import json
import logging
import discord
from discord import app_commands
from discord.ext import commands
import secrets
import aiohttp
from aiohttp import web

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DiscordManager")

# Configuração Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ojjfwxjirlttpxcjhlho.supabase.co").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9qamZ3eGppcmx0dHB4Y2pobGhvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODkyMjExMjcsImV4cCI6MjEwNDc5NzEyN30.QiBcBHLwS2yWbmgi_oAKSmRU1UEFNRXgfyLujmEK7XU")
SUPABASE_TABLE = "discord_server_keys"

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


async def execute_clone_roles(source_guild: discord.Guild, target_guild: discord.Guild) -> tuple[dict, int]:
    role_map = {source_guild.default_role: target_guild.default_role}
    try:
        await target_guild.default_role.edit(permissions=source_guild.default_role.permissions)
    except Exception:
        pass

    roles = [r for r in source_guild.roles if not r.is_default() and not r.managed]
    roles.sort(key=lambda r: r.position)
    existing_roles = {r.name: r for r in target_guild.roles if not r.managed}

    created_count = 0
    for role in roles:
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
            created_count += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro ao criar cargo {role.name}: {e}")

    return role_map, created_count


async def execute_clone_channels(source_guild: discord.Guild, target_guild: discord.Guild, role_map: dict = None) -> tuple[int, int]:
    if role_map is None:
        role_map = {source_guild.default_role: target_guild.default_role}
        target_roles = {r.name: r for r in target_guild.roles}
        for r in source_guild.roles:
            if r.name in target_roles:
                role_map[r] = target_roles[r.name]

    cat_map = {}
    categories_created = 0
    channels_created = 0

    for cat in sorted(source_guild.categories, key=lambda c: c.position):
        try:
            overwrites = build_overwrites(cat.overwrites, role_map, target_guild)
            new_cat = await target_guild.create_category(name=cat.name, overwrites=overwrites)
            cat_map[cat.id] = new_cat
            categories_created += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro na categoria {cat.name}: {e}")

    for ch in sorted(source_guild.text_channels, key=lambda c: c.position):
        try:
            parent = cat_map.get(ch.category_id) if ch.category_id else None
            overwrites = build_overwrites(ch.overwrites, role_map, target_guild)
            await target_guild.create_text_channel(
                name=ch.name,
                category=parent,
                topic=ch.topic,
                slowmode_delay=ch.slowmode_delay,
                nsfw=ch.nsfw,
                overwrites=overwrites
            )
            channels_created += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro no canal #{ch.name}: {e}")

    for vc in sorted(source_guild.voice_channels, key=lambda c: c.position):
        try:
            parent = cat_map.get(vc.category_id) if vc.category_id else None
            overwrites = build_overwrites(vc.overwrites, role_map, target_guild)
            bitrate = min(vc.bitrate, int(target_guild.bitrate_limit))
            await target_guild.create_voice_channel(
                name=vc.name,
                category=parent,
                bitrate=bitrate,
                user_limit=vc.user_limit,
                overwrites=overwrites
            )
            channels_created += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro no canal de voz {vc.name}: {e}")

    return categories_created, channels_created


async def execute_clone_emojis(source_guild: discord.Guild, target_guild: discord.Guild) -> int:
    existing = {e.name for e in target_guild.emojis}
    copied = 0
    for emoji in source_guild.emojis:
        if emoji.name in existing:
            continue
        try:
            img = await emoji.read()
            await target_guild.create_custom_emoji(name=emoji.name, image=img)
            copied += 1
            await asyncio.sleep(0.5)
        except discord.HTTPException as e:
            if e.code == 30008:
                break
        except Exception:
            pass
    return copied


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
            "• `/gerar_id` ou `!gerar_id` — Gera um novo código secreto\n"
            "• `!sync` — Força registro imediato dos comandos /\n"
            "• `/clonar_tudo <código>` — Clona tudo (exige código secreto)\n"
            "• `/clonar_cargos <código>` — Clona só cargos\n"
            "• `/clonar_canais <código>` — Clona só canais\n"
            "• `/clonar_emojis <código>` — Clona emojis\n"
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
@app_commands.describe(id_origem="Código (ex: SRV-XXXXXX) ou ID do servidor de onde você quer copiar")
@app_commands.default_permissions(administrator=True)
async def cmd_clonar_tudo(interaction: discord.Interaction, id_origem: str):
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
        f"Isso criará cargos, categorias, canais e emojis idênticos.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        msg = await interaction.followup.send("🚀 [1/3] Iniciando clonagem de cargos...", ephemeral=True)
        role_map, roles_cnt = await execute_clone_roles(source_guild, target_guild)
        await msg.edit(content=f"✅ Cargos clonados ({roles_cnt}).\n🚀 [2/3] Clonando categorias e canais...")
        cats_cnt, chs_cnt = await execute_clone_channels(source_guild, target_guild, role_map)
        await msg.edit(content=f"✅ Cargos ({roles_cnt}) e Canais ({chs_cnt}) clonados.\n🚀 [3/3] Clonando emojis...")
        emojis_cnt = await execute_clone_emojis(source_guild, target_guild)
        await msg.edit(content=f"🎉 **Clonagem Completa Concluída!**\n• {roles_cnt} Cargos\n• {cats_cnt} Categorias\n• {chs_cnt} Canais\n• {emojis_cnt} Emojis")


@bot.tree.command(name="clonar_cargos", description="Clona APENAS os cargos de outro servidor.")
@app_commands.describe(id_origem="Código (ex: SRV-XXXXXX) ou ID do servidor de origem")
@app_commands.default_permissions(administrator=True)
async def cmd_clonar_cargos(interaction: discord.Interaction, id_origem: str):
    if not await check_admin_permission(interaction):
        return

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await interaction.response.send_message(f"❌ {err_msg}", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    _, roles_cnt = await execute_clone_roles(source_guild, interaction.guild)
    await interaction.followup.send(f"✅ **Sucesso:** {roles_cnt} cargos clonados de **{source_guild.name}**!", ephemeral=True)


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
    cats_cnt, chs_cnt = await execute_clone_channels(source_guild, interaction.guild)
    await interaction.followup.send(f"✅ **Sucesso:** {cats_cnt} categorias e {chs_cnt} canais clonados de **{source_guild.name}**!", ephemeral=True)


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
    count = await execute_clone_emojis(source_guild, interaction.guild)
    await interaction.followup.send(f"✅ **Sucesso:** {count} emojis copiados de **{source_guild.name}**!", ephemeral=True)


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

        await msg.edit(content=f"✅ Categoria **{categoria.name}** e todos os seus {channels_count} canais foram apagados!")


@bot.tree.command(name="limpar_canais", description="🚨 Reseta o servidor: Apaga TODOS os canais existentes.")
@app_commands.default_permissions(administrator=True)
async def cmd_limpar_canais(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    guild = interaction.guild
    view = ConfirmDangerAction(interaction.user.id, "Limpar Todos os Canais")

    await interaction.response.send_message(
        f"🚨 **PERIGO MÁXIMO:** Você está prestes a apagar **TODOS OS {len(guild.channels)} CANAIS** de **{guild.name}**!\n"
        f"Esta ação não pode ser desfeita.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        temp_ch = await guild.create_text_channel(name="suporte-reset", reason="Canal temporário durante reset")
        for ch in list(guild.channels):
            if ch.id == temp_ch.id:
                continue
            try:
                await ch.delete(reason="Reset geral de canais")
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await temp_ch.send("✅ **Reset de canais concluído!** Apenas este canal foi mantido para você continuar.")


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
        deleted = 0
        for r in roles:
            try:
                await r.delete(reason="Reset de cargos solicitado por administrador")
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await interaction.followup.send(f"✅ Reset concluído! {deleted} cargos foram excluídos.", ephemeral=True)


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
    """Sincroniza os comandos Slash no servidor atual."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")

    msg = await ctx.reply("⏳ Sincronizando comandos slash neste servidor...")
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(content=f"✅ Sincronizados {len(synced)} comandos slash com sucesso neste servidor! Pressione `Ctrl + R` se ainda não aparecerem.")
    except Exception as e:
        await msg.edit(content=f"❌ Erro ao sincronizar: {e}")


@bot.command(name="clonar_tudo")
async def prefix_clonar_tudo(ctx: commands.Context, id_origem: str = None):
    """Clona tudo via comando de prefixo !clonar_tudo <id ou código>."""
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    if not id_origem:
        return await ctx.reply("❌ Use: `!clonar_tudo <código ou id_do_servidor_origem>`")

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await ctx.reply(f"❌ {err_msg}")

    msg = await ctx.reply(f"🚀 Iniciando clonagem completa de **{source_guild.name}**...")
    role_map, roles_cnt = await execute_clone_roles(source_guild, ctx.guild)
    cats_cnt, chs_cnt = await execute_clone_channels(source_guild, ctx.guild, role_map)
    emojis_cnt = await execute_clone_emojis(source_guild, ctx.guild)
    await msg.edit(content=f"🎉 **Clonagem Concluída!**\n• {roles_cnt} Cargos\n• {cats_cnt} Categorias\n• {chs_cnt} Canais\n• {emojis_cnt} Emojis")


@bot.command(name="clonar_cargos")
async def prefix_clonar_cargos(ctx: commands.Context, id_origem: str = None):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")
    if not id_origem:
        return await ctx.reply("❌ Use: `!clonar_cargos <código ou id_do_servidor_origem>`")

    source_guild, err_msg = await resolve_source_guild(id_origem)
    if err_msg:
        return await ctx.reply(f"❌ {err_msg}")

    msg = await ctx.reply(f"🚀 Clonando cargos de **{source_guild.name}**...")
    _, roles_cnt = await execute_clone_roles(source_guild, ctx.guild)
    await msg.edit(content=f"✅ Sucesso! {roles_cnt} cargos clonados de **{source_guild.name}**.")


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
    cats_cnt, chs_cnt = await execute_clone_channels(source_guild, ctx.guild)
    await msg.edit(content=f"✅ Sucesso! {cats_cnt} categorias e {chs_cnt} canais clonados.")


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
    count = await execute_clone_emojis(source_guild, ctx.guild)
    await msg.edit(content=f"✅ Sucesso! {count} emojis copiados.")


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

    await msg.edit(content=f"✅ Categoria **{cat_target.name}** e todos os seus {channels_count} canais foram apagados!")


@bot.command(name="limpar_canais")
async def prefix_limpar_canais(ctx: commands.Context):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")

    view = ConfirmDangerAction(ctx.author.id, "Limpar Canais")
    confirm_msg = await ctx.reply("⚠️ **ATENÇÃO:** Isso apagará TODOS os canais deste servidor! Confirma?", view=view)
    await view.wait()

    if view.value:
        temp_ch = await ctx.guild.create_text_channel(name="suporte-reset", reason="Canal temporário durante reset")
        for ch in list(ctx.guild.channels):
            if ch.id == temp_ch.id:
                continue
            try:
                await ch.delete(reason="Reset geral de canais")
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await temp_ch.send("✅ **Reset de canais concluído!** Apenas este canal foi mantido para você continuar.")


@bot.command(name="limpar_cargos")
async def prefix_limpar_cargos(ctx: commands.Context):
    if not is_authorized_admin(ctx):
        return await ctx.reply("⛔ Acesso negado.")

    roles = [r for r in ctx.guild.roles if not r.is_default() and not r.managed and r < ctx.guild.me.top_role]
    view = ConfirmDangerAction(ctx.author.id, "Limpar Cargos")
    confirm_msg = await ctx.reply(f"⚠️ **ATENÇÃO:** Isso apagará {len(roles)} cargos personalizados! Confirma?", view=view)
    await view.wait()

    if view.value:
        deleted = 0
        for r in roles:
            try:
                await r.delete(reason="Reset de cargos solicitado por administrador")
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await ctx.reply(f"✅ Reset concluído! {deleted} cargos foram excluídos.")


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

    # Sincroniza os comandos Slash em cada servidor e destaca cargo no tab
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            logger.info(f"Comandos sincronizados no servidor: {guild.name}")
        except Exception as e:
            logger.warning(f"Erro ao sincronizar na guild {guild.name}: {e}")

        # Tenta exibir o cargo do bot destacado no tab (hoist=True)
        try:
            bot_member = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
            if bot_member and bot_member.guild_permissions.manage_roles:
                top_r = bot_member.top_role
                if top_r and not top_r.is_default() and not top_r.hoist:
                    await top_r.edit(hoist=True, reason="Destacar ServerManager no tab de membros")
        except Exception:
            pass

    try:
        await bot.tree.sync()
    except Exception:
        pass

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

    # 2. Sincroniza Slash Commands no novo servidor
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info(f"Comandos sincronizados no novo servidor: {guild.name}")
    except Exception as e:
        logger.warning(f"Erro ao sincronizar novo servidor: {e}")

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
