from pathlib import Path
from odoo_agent_server.services.rea_api_inspect_service import ReaApiInspectService

def test_api_inspector_finds_calls_routes_and_config_without_network(tmp_path):
    addon=tmp_path/"demo"; (addon/"models").mkdir(parents=True)
    (addon/"__manifest__.py").write_text("{'name':'Demo','depends':['base']}",encoding="utf-8")
    (addon/"models"/"integration.py").write_text(
        "import requests\n"
        "API_BASE_URL = 'https://api.example.test/v1'\n"
        "API_TOKEN = os.getenv('DEMO_API_TOKEN')\n"
        "def send():\n"
        "    return requests.post(API_BASE_URL, json={}, timeout=15, verify=False, headers={'Authorization': API_TOKEN})\n"
        "@http.route('/demo/webhook', type='json', auth='public', methods=['POST'])\n"
        "def webhook(self):\n"
        "    return True\n",encoding="utf-8")
    result=ReaApiInspectService().inspect(module_path=addon,module_name="demo")
    kinds={x["type"] for x in result["findings"]}
    assert {"outbound_client_call","url_or_endpoint_config","credential_config_reference","webhook_or_http_route"} <= kinds
    call=next(x for x in result["findings"] if x["type"]=="outbound_client_call")
    assert call["timeout"]=="15" and call["tls_verify"]=="False"
    assert result["safety"]["network_requests_made"] is False

def test_api_inspector_ignores_non_python_and_is_bounded(tmp_path):
    addon=tmp_path/"demo"; (addon/"models").mkdir(parents=True)
    (addon/"__manifest__.py").write_text("{'name':'Demo','depends':['base']}",encoding="utf-8")
    (addon/"static"/"data.txt").mkdir(parents=True)
    (addon/"static"/"data.txt"/"ignore.py").write_text("requests.get('https://ignore.test')",encoding="utf-8")
    result=ReaApiInspectService().inspect(module_path=addon,module_name="demo")
    assert result["summary"]["outbound_calls"]==0
