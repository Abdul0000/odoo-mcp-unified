from pathlib import Path
from odoo_agent_server.services.rea_upstream_service import ReaUpstreamService

def _addon(root:Path,name:str,source:str)->Path:
    path=root/name; (path/"models").mkdir(parents=True)
    (path/"__manifest__.py").write_text("{'name':'Addon','depends':['base']}",encoding="utf-8")
    (path/"models"/"model.py").write_text(source,encoding="utf-8"); return path

def test_upstream_compare_reports_signature_and_field_drift(tmp_path,monkeypatch):
    custom=_addon(tmp_path/"custom","my_addon","from odoo import models,fields\nclass P(models.Model):\n _inherit='res.partner'\n name=fields.Integer()\n def write(self, vals, extra=None):\n  return super().write(vals)\n")
    _addon(tmp_path/"core","base","from odoo import models,fields\nclass P(models.Model):\n _name='res.partner'\n name=fields.Char()\n def write(self, vals):\n  return super().write(vals)\n")
    monkeypatch.setattr("odoo_agent_server.services.rea_upstream_service.CoreFinderService._resolve_roots",lambda self,search_roots=None,client_name=None:[(1,str(tmp_path/"core"))])
    result=ReaUpstreamService().compare(module_path=custom,module_name="my_addon")
    codes={x["code"] for x in result["findings"]}
    assert {"METHOD_SIGNATURE_DRIFT","FIELD_TYPE_DRIFT"} <= codes
    assert result["findings"][0]["evidence"]["upstream_path"].startswith("base/")

def test_upstream_compare_is_bounded(tmp_path,monkeypatch):
    custom=_addon(tmp_path/"custom","my_addon","from odoo import models\nclass P(models.Model):\n _inherit='res.partner'\n")
    base=_addon(tmp_path/"core","base","from odoo import models\nclass P(models.Model):\n _name='res.partner'\n")
    (base/"models"/"large.py").write_text("value='"+ "x"*5000 +"'",encoding="utf-8")
    monkeypatch.setattr("odoo_agent_server.services.rea_upstream_service.CoreFinderService._resolve_roots",lambda self,search_roots=None,client_name=None:[(1,str(tmp_path/"core"))])
    result=ReaUpstreamService().compare(module_path=custom,module_name="my_addon",max_total_chars=4000)
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["dependency_files_omitted"] >= 1
