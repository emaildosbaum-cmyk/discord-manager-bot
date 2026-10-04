import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import discord

import main

class TestRevertSystem(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Limpa histórico para testes isolados
        main._server_history = {}
        if os.path.exists(main.HISTORY_FILE):
            try:
                os.remove(main.HISTORY_FILE)
            except Exception:
                pass

    def tearDown(self):
        if os.path.exists(main.HISTORY_FILE):
            try:
                os.remove(main.HISTORY_FILE)
            except Exception:
                pass

    def test_record_and_get_last_action(self):
        guild_id = 123456789
        action = {
            "type": "clonar_cargos",
            "name": "Clonagem de Cargos",
            "details": "3 Cargos",
            "created_role_ids": [101, 102, 103],
            "timestamp": 1000.0,
            "author_id": 999
        }
        main.record_last_action(guild_id, action)

        retrieved = main.get_last_action(guild_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["type"], "clonar_cargos")
        self.assertEqual(retrieved["created_role_ids"], [101, 102, 103])

        # Teste de persistência no arquivo
        main._server_history = {}
        main.load_history()
        reloaded = main.get_last_action(guild_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["details"], "3 Cargos")

        # Teste de limpeza após reversão
        main.clear_last_action(guild_id)
        self.assertIsNone(main.get_last_action(guild_id))

    async def test_revert_clonar_cargos(self):
        guild = MagicMock(spec=discord.Guild)
        role1 = MagicMock(spec=discord.Role)
        role1.delete = AsyncMock()
        role2 = MagicMock(spec=discord.Role)
        role2.delete = AsyncMock()

        def mock_get_role(role_id):
            if role_id == 101:
                return role1
            if role_id == 102:
                return role2
            return None

        guild.get_role.side_effect = mock_get_role

        action = {
            "type": "clonar_cargos",
            "name": "Clonagem de Cargos",
            "created_role_ids": [101, 102]
        }

        success, result_text = await main.execute_revert_action(guild, action)
        self.assertTrue(success)
        self.assertIn("2 Cargos removidos", result_text)
        role1.delete.assert_awaited_once()
        role2.delete.assert_awaited_once()

    async def test_revert_clonar_tudo(self):
        guild = MagicMock(spec=discord.Guild)

        # Mocks para cargos, canais, categorias e emojis
        role = MagicMock(spec=discord.Role)
        role.delete = AsyncMock()
        guild.get_role.return_value = role

        cat = MagicMock(spec=discord.CategoryChannel)
        cat.delete = AsyncMock()
        ch = MagicMock(spec=discord.TextChannel)
        ch.delete = AsyncMock()

        def mock_get_channel(cid):
            if cid == 301:
                return cat
            if cid == 401:
                return ch
            return None

        guild.get_channel.side_effect = mock_get_channel

        emoji = MagicMock(spec=discord.Emoji)
        emoji.delete = AsyncMock()
        guild.get_emoji.return_value = emoji

        action = {
            "type": "clonar_tudo",
            "name": "Clonagem Completa",
            "created_role_ids": [101],
            "created_category_ids": [301],
            "created_channel_ids": [401],
            "created_emoji_ids": [501]
        }

        success, result_text = await main.execute_revert_action(guild, action)
        self.assertTrue(success)
        self.assertIn("1 Cargos removidos", result_text)
        self.assertIn("1 Categorias removidas", result_text)
        self.assertIn("1 Canais removidos", result_text)
        self.assertIn("1 Emojis removidos", result_text)

        role.delete.assert_awaited_once()
        cat.delete.assert_awaited_once()
        ch.delete.assert_awaited_once()
        emoji.delete.assert_awaited_once()

    async def test_revert_limpar_cargos(self):
        guild = MagicMock(spec=discord.Guild)
        guild.create_role = AsyncMock()

        action = {
            "type": "limpar_cargos",
            "name": "Reset de Cargos",
            "snapshot_roles": [
                {"name": "VIP", "permissions": 0, "color": 0xFF0000, "hoist": True, "mentionable": False},
                {"name": "Membro", "permissions": 0, "color": 0x00FF00, "hoist": False, "mentionable": True}
            ]
        }

        success, result_text = await main.execute_revert_action(guild, action)
        self.assertTrue(success)
        self.assertIn("2 de 2 cargos foram restaurados", result_text)
        self.assertEqual(guild.create_role.await_count, 2)

    async def test_revert_limpar_canais(self):
        guild = MagicMock(spec=discord.Guild)
        guild.create_text_channel = AsyncMock()
        guild.create_voice_channel = AsyncMock()

        action = {
            "type": "limpar_canais",
            "name": "Reset Geral de Canais",
            "snapshot_channels": [
                {"name": "geral", "type": "text", "topic": "Bate-papo"},
                {"name": "Call 1", "type": "voice"}
            ]
        }

        success, result_text = await main.execute_revert_action(guild, action)
        self.assertTrue(success)
        self.assertIn("2 canais restaurados", result_text)
        guild.create_text_channel.assert_awaited_once()
        guild.create_voice_channel.assert_awaited_once()

    async def test_revert_apagar_categoria(self):
        guild = MagicMock(spec=discord.Guild)
        mock_cat = MagicMock(spec=discord.CategoryChannel)
        guild.create_category = AsyncMock(return_value=mock_cat)
        guild.create_text_channel = AsyncMock()

        action = {
            "type": "apagar_categoria",
            "name": "Exclusão da Categoria Comunidade",
            "snapshot_category": {
                "name": "Comunidade",
                "channels": [
                    {"name": "anúncios", "type": "text"}
                ]
            }
        }

        success, result_text = await main.execute_revert_action(guild, action)
        self.assertTrue(success)
        self.assertIn("1 canais restaurados", result_text)
        guild.create_category.assert_awaited_once_with(name="Comunidade", reason="Reversão de exclusão de categoria")
        guild.create_text_channel.assert_awaited_once()

    def test_is_color_role_detection(self):
        # 1. Cargos da screenshot do usuário e cores CSS válidas
        for color_name in ["LightSalmon", "DarkSalmon", "Crimsom", "HotPink", "DeepPink", "Plum", "DarkRed"]:
            role = MagicMock(spec=discord.Role)
            role.is_default.return_value = False
            role.managed = False
            role.name = color_name
            role.color = discord.Color(0xFF5555)
            role.permissions = discord.Permissions(0)
            self.assertTrue(main.is_color_role(role), f"Deveria detectar {color_name} como cargo de cor")

        # 2. Formato Hexadecimal e prefixos
        for hex_or_prefix in ["#ff5733", "00aabb", "cor-azul", "color red", "c-pink"]:
            role = MagicMock(spec=discord.Role)
            role.is_default.return_value = False
            role.managed = False
            role.name = hex_or_prefix
            role.color = discord.Color(0x334455)
            role.permissions = discord.Permissions(0)
            self.assertTrue(main.is_color_role(role), f"Deveria detectar {hex_or_prefix} como cargo de cor")

        # 3. SEGURANÇA: Cargo com mesmo nome de cor, mas com permissão de administrador ou staff
        admin_role = MagicMock(spec=discord.Role)
        admin_role.is_default.return_value = False
        admin_role.managed = False
        admin_role.name = "Crimson"
        admin_role.color = discord.Color(0xDC143C)
        admin_perms = discord.Permissions()
        admin_perms.administrator = True
        admin_role.permissions = admin_perms
        self.assertFalse(main.is_color_role(admin_role), "NUNCA deve detectar cargo com permissões administrativas como cargo de cor!")

        # 4. SEGURANÇA: Cargo sem cor atribuída (default 0)
        no_color_role = MagicMock(spec=discord.Role)
        no_color_role.is_default.return_value = False
        no_color_role.managed = False
        no_color_role.name = "Plum"
        no_color_role.color = discord.Color(0)
        no_color_role.permissions = discord.Permissions(0)
        self.assertFalse(main.is_color_role(no_color_role), "Cargo sem cor personalizada não deve ser detectado")

        # 5. Cargos normais de servidor (VIP, Dono, Moderador, Membro)
        for normal_name in ["VIP", "Dono", "Moderador", "Membro", "Gamer"]:
            role = MagicMock(spec=discord.Role)
            role.is_default.return_value = False
            role.managed = False
            role.name = normal_name
            role.color = discord.Color(0x123456)
            role.permissions = discord.Permissions(0)
            self.assertFalse(main.is_color_role(role), f"{normal_name} não deve ser detectado como cargo de cor")

    async def test_clone_roles_ignorar_cores(self):
        source_guild = MagicMock(spec=discord.Guild)
        target_guild = MagicMock(spec=discord.Guild)
        source_guild.name = "Origem"
        target_guild.name = "Destino"
        source_guild.default_role = MagicMock()
        target_guild.default_role = MagicMock()
        target_guild.default_role.edit = AsyncMock()

        # Cria 1 cargo normal e 2 cargos de cor
        role_normal = MagicMock(spec=discord.Role)
        role_normal.name = "Membro VIP"
        role_normal.position = 1
        role_normal.managed = False
        role_normal.is_default.return_value = False
        role_normal.color = discord.Color(0x111111)
        role_normal.permissions = discord.Permissions(0)
        role_normal.hoist = False
        role_normal.mentionable = False

        role_color1 = MagicMock(spec=discord.Role)
        role_color1.name = "HotPink"
        role_color1.position = 2
        role_color1.managed = False
        role_color1.is_default.return_value = False
        role_color1.color = discord.Color(0xFF69B4)
        role_color1.permissions = discord.Permissions(0)
        role_color1.hoist = False
        role_color1.mentionable = False

        role_color2 = MagicMock(spec=discord.Role)
        role_color2.name = "LightSalmon"
        role_color2.position = 3
        role_color2.managed = False
        role_color2.is_default.return_value = False
        role_color2.color = discord.Color(0xFFA07A)
        role_color2.permissions = discord.Permissions(0)
        role_color2.hoist = False
        role_color2.mentionable = False

        source_guild.roles = [source_guild.default_role, role_normal, role_color1, role_color2]
        target_guild.roles = [target_guild.default_role]

        target_guild.create_role = AsyncMock(side_effect=lambda **kwargs: MagicMock(spec=discord.Role, name=kwargs.get("name")))

        # Executa com ignorar_cores=True
        role_map, created_roles = await main.execute_clone_roles(source_guild, target_guild, ignorar_cores=True)

        self.assertEqual(len(created_roles), 1)
        target_guild.create_role.assert_awaited_once()
        call_kwargs = target_guild.create_role.await_args.kwargs
        self.assertEqual(call_kwargs["name"], "Membro VIP")

    async def test_execute_wipe_guild(self):
        guild = MagicMock(spec=discord.Guild)
        guild.default_role = MagicMock()
        guild.me = MagicMock()
        guild.me.top_role = MagicMock()
        guild.me.top_role.position = 999

        # Cargos
        role1 = MagicMock(spec=discord.Role)
        role1.name = "Role Antiga"
        role1.position = 10
        role1.managed = False
        role1.is_default.return_value = False
        role1.permissions.value = 0
        role1.color.value = 0x112233
        role1.hoist = False
        role1.mentionable = False
        role1.delete = AsyncMock()

        guild.roles = [guild.default_role, role1]

        # Canais
        ch1 = MagicMock(spec=discord.TextChannel)
        ch1.id = 111
        ch1.name = "canal-antigo"
        ch1.topic = "antigo"
        ch1.delete = AsyncMock()

        guild.channels = [ch1]

        # Emojis
        em1 = MagicMock(spec=discord.Emoji)
        em1.delete = AsyncMock()
        guild.emojis = [em1]

        temp_ch = MagicMock(spec=discord.TextChannel)
        temp_ch.id = 999
        guild.create_text_channel = AsyncMock(return_value=temp_ch)

        snap_roles, snap_chs, returned_temp = await main.execute_wipe_guild(guild)

        self.assertEqual(len(snap_roles), 1)
        self.assertEqual(snap_roles[0]["name"], "Role Antiga")
        self.assertEqual(len(snap_chs), 1)
        self.assertEqual(snap_chs[0]["name"], "canal-antigo")
        self.assertEqual(returned_temp, temp_ch)

        role1.delete.assert_awaited_once()
        ch1.delete.assert_awaited_once()
        em1.delete.assert_awaited_once()

    async def test_revert_clonar_tudo_com_limpeza_previa(self):
        guild = MagicMock(spec=discord.Guild)
        guild.create_role = AsyncMock()
        guild.create_text_channel = AsyncMock()

        # Item clonado que deve ser apagado
        cloned_role = MagicMock(spec=discord.Role)
        cloned_role.delete = AsyncMock()
        guild.get_role.return_value = cloned_role

        action = {
            "type": "clonar_tudo",
            "name": "Clonagem Completa",
            "created_role_ids": [101],
            "created_category_ids": [],
            "created_channel_ids": [],
            "created_emoji_ids": [],
            "limpou_antes": True,
            "snapshot_roles": [
                {"name": "Original Role", "permissions": 0, "color": 0xFF0000, "hoist": False, "mentionable": False}
            ],
            "snapshot_channels": [
                {"name": "original-chat", "type": "text", "topic": "antigo"}
            ]
        }

        success, result_text = await main.execute_revert_action(guild, action)
        self.assertTrue(success)
        self.assertIn("1 Cargos removidos", result_text)
        self.assertIn("1 Cargos anteriores restaurados", result_text)
        self.assertIn("1 Canais anteriores restaurados", result_text)

        cloned_role.delete.assert_awaited_once()
        guild.create_role.assert_awaited_once()
        guild.create_text_channel.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()


