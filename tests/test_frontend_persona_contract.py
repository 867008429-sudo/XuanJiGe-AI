import re
import unittest

import app


class FrontendPersonaContractTests(unittest.TestCase):
    """Static contract checks for the inline user-facing consultation UI."""

    @classmethod
    def setUpClass(cls):
        cls.html = app.INDEX_HTML
        cls.scripts = re.findall(r'<script>(.*?)</script>', cls.html, flags=re.S)
        cls.main_script = cls.scripts[-1]
        visible = re.sub(r'<script>.*?</script>', '', cls.html, flags=re.S)
        visible = re.sub(r'<style>.*?</style>', '', visible, flags=re.S)
        visible = re.sub(r'<!--.*?-->', '', visible, flags=re.S)
        cls.visible_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', visible))

    def test_persona_bar_markup_and_incense_copy_exist(self):
        self.assertIn('id="personaBar"', self.html)
        self.assertIn('aria-label="选择请教的道长"', self.html)
        self.assertIn('每个命盘赠 10 炷香火', self.html)
        self.assertIn('各道长每问耗香不同', self.html)

    def test_user_facing_consultation_shell_exists(self):
        self.assertIn('class="container agent-container"', self.html)
        self.assertIn('class="agent-shell"', self.html)
        self.assertIn('aria-label="命盘咨询区"', self.html)
        self.assertIn('aria-label="咨询说明"', self.html)
        self.assertIn('命盘咨询间', self.visible_text)
        self.assertIn('先立命盘，再慢慢追问', self.visible_text)
        self.assertIn('继续追问', self.visible_text)

    def test_backstage_terms_are_not_user_visible(self):
        backstage_terms = [
            'Agent Console',
            'Grounding',
            'P4',
            '运行面板',
            '内测指标',
            'chunk_id',
            'search_classics',
            'checkpoint',
            'issue code',
        ]
        for term in backstage_terms:
            self.assertNotIn(term, self.visible_text)

    def test_chat_is_promoted_before_report_tables(self):
        result_idx = self.html.index('id="resultSection"')
        chat_idx = self.html.index('id="chatSection"')
        bazi_idx = self.html.index('class="bazi-table"')
        self.assertGreater(chat_idx, result_idx)
        self.assertLess(chat_idx, bazi_idx)

    def test_send_chat_payload_carries_selected_persona(self):
        self.assertIn("persona: chatPersonaId || ''", self.main_script)
        self.assertIn("body: JSON.stringify({", self.main_script)
        self.assertIn("hid: activeHid", self.main_script)
        self.assertIn("request_id: requestId", self.main_script)

    def test_forbidden_response_uses_backend_incense_detail(self):
        forbidden_block = self._extract_between(
            "if (res.status === 403) {",
            "if (!res.ok || !res.body) {",
        )
        self.assertIn("let msg = '香火已尽，可回看已生成的报告。'", forbidden_block)
        self.assertIn("if (data && data.message) msg = data.message", forbidden_block)
        self.assertIn("showChatError(msg)", forbidden_block)

    def test_done_event_appends_persona_incense_note(self):
        done_block = self._extract_between(
            "} else if (chunk.persona && chunk.price) {",
            "} else if (chunk.type === 'error') {",
        )
        self.assertIn("personaTitle(chunk.persona)", done_block)
        self.assertIn("'请教' + t + '，上香 ' + chunk.price + ' 炷。'", done_block)

    def test_done_event_attaches_helpful_feedback(self):
        done_block = self._extract_between(
            "} else if (chunk.type === 'done') {",
            "} else if (chunk.type === 'error') {",
        )
        self.assertIn("const hasAssistantText = assistantEl.textContent.trim().length > 0", done_block)
        self.assertIn("if (!hasAssistantText)", done_block)
        self.assertIn("attachChatFeedback(assistantEl, activeHid, chunk.request_id || requestId)", done_block)
        self.assertIn("function submitChatFeedback(hid, requestId, rating, controls)", self.main_script)
        self.assertIn("'/api/chat/feedback'", self.main_script)

    def test_main_script_uses_safe_storage_wrapper(self):
        self.assertIn('function safeStorageGet(key)', self.main_script)
        self.assertIn('function safeStorageSet(key, value)', self.main_script)
        self.assertIn('function safeStorageRemove(key)', self.main_script)
        unsafe_lines = [
            line.strip()
            for line in self.main_script.splitlines()
            if 'localStorage.' in line and 'window.localStorage' not in line
        ]
        self.assertEqual([], unsafe_lines)

    def test_display_result_hides_agent_empty_state(self):
        display_block = self._extract_between(
            'function displayResult(r) {',
            'const fp = r.four_pillars;',
        )
        self.assertIn("document.getElementById('agentEmptyState')", display_block)
        self.assertIn("emptyState.style.display = 'none'", display_block)

    def test_consultation_mobile_overflow_guards_exist(self):
        self.assertIn('.agent-workbench > *, .result-section > * { min-width: 0; max-width: 100%; }', self.html)
        start_index = self.html.index('.chat-send-btn {')
        end_index = self.html.index('.chat-send-btn:disabled', start_index)
        send_btn_css = self.html[start_index:end_index]
        self.assertIn('width: auto', send_btn_css)
        self.assertIn('margin-top: 0', send_btn_css)
        self.assertIn('flex: 0 0 auto', send_btn_css)

    def _extract_between(self, start, end):
        start_index = self.main_script.index(start)
        end_index = self.main_script.index(end, start_index)
        return self.main_script[start_index:end_index]


if __name__ == '__main__':
    unittest.main()
