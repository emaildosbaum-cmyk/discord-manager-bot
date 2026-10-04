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

if __name__ == "__main__":
    unittest.main()
