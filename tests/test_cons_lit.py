"""Regresiones de Cons. Lit. con Chrome local; no requieren acceso a SITFA."""

import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import main
from browser_compat import By, PlaywrightDriver


def popup_html(subject="DDO.", disabled=False, invalid_url=False):
    # Misma estructura que el popup real: fila exterior de maquetación,
    # celdas vacías, varias filas SelectItem y botón inicialmente deshabilitado.
    action = "/popup?RUT_Litigante=&RUT_DV=&CRR_IdParte=&COD_Litigante=" if invalid_url else (
        "/popup?RUT_Litigante=" + "' + rut + '&RUT_DV=' + dv + '&CRR_IdParte=' + parte + '&COD_Litigante=' + cod + '"
    )
    return f"""
    <script>
    var rut='', dv='', parte='', cod='';
    function SelectItem(row,r,d,p,c,i,x,e,t) {{
        rut=r; dv=d; parte=p; cod=c;
        document.getElementById('consLitigante').disabled={str(disabled).lower()};
    }}
    function ShowPopUp() {{ window.open('{action}'); }}
    </script>
    <table id='layout'><tr><td><table id='litigantes'>
    <tr><th>Est</th><th>Sujeto</th><th>Rut/Pasaporte</th><th>Nombre</th></tr>
    <tr onclick="SelectItem(this,'11111111','1','100','1','0','0','0','N')">
    <td></td><td>DTE.</td><td>11.111.111-1</td><td>Demandante de prueba</td></tr>
    <tr onclick="SelectItem(this,'22222222','0','200','2','0','0','0','N')">
    <td></td><td>{subject}</td><td>22.222.222-0</td><td>Demandado de prueba</td></tr>
    </table></td></tr></table>
    <input id='consLitigante' name='ConsLitiganteButton' type='button'
           value='Cons. Lit.' disabled onclick='ShowPopUp()'>
    """


class BrowserRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = PlaywrightDriver(browser="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def setUp(self):
        self.driver.switch_to.default_content()

    def test_find_elements_preserves_selected_parent(self):
        self.driver._page.set_content("<table><tr><td>primero</td></tr><tr><td>segundo</td><td>tercero</td></tr></table>")
        rows = self.driver.find_elements(By.TAG_NAME, "tr")
        self.assertEqual([cell.text for cell in rows[1].find_elements(By.TAG_NAME, "td")], ["segundo", "tercero"])
        self.assertEqual(rows[1].find_element(By.TAG_NAME, "td").text, "segundo")

    def test_recursive_parent_and_child_indexes(self):
        self.driver._page.set_content("<div><p><span>uno</span></p></div><div><p><span>dos</span></p><p><span>tres</span></p></div>")
        div = self.driver.find_elements(By.TAG_NAME, "div")[1]
        paragraph = div.find_elements(By.TAG_NAME, "p")[1]
        self.assertEqual(paragraph.find_element(By.TAG_NAME, "span").text, "tres")

    def test_script_receives_dom_handle_from_selected_child(self):
        self.driver._page.set_content("<table><tr><td>primero</td></tr><tr><td>segundo</td></tr></table>")
        child = self.driver.find_elements(By.TAG_NAME, "tr")[1].find_element(By.TAG_NAME, "td")
        self.assertEqual(self.driver.execute_script("return arguments[0].textContent;", child), "segundo")

    def test_scoped_children_in_iframe(self):
        self.driver._page.set_content('''<iframe srcdoc="<table><tr><td>uno</td></tr><tr><td>dos</td></tr></table>"></iframe>''')
        self.driver.switch_to.frame(0)
        rows = self.driver.find_elements(By.TAG_NAME, "tr")
        self.assertEqual(rows[1].find_element(By.TAG_NAME, "td").text, "dos")
        self.driver.switch_to.default_content()

    def test_selects_real_demandado_in_nested_table(self):
        for subject in ("DDO.", "DDO", " DDO. "):
            with self.subTest(subject=subject):
                self.driver._page.set_content(popup_html(subject))
                url, reason = main.open_cons_lit_popup_from_litigantes(self.driver)
                self.assertEqual(reason, "")
                query = parse_qs(urlparse(url).query)
                self.assertEqual(query['RUT_Litigante'], ['22222222'])
                self.assertEqual(query['RUT_DV'], ['0'])
                self.assertEqual(query['CRR_IdParte'], ['200'])
                self.assertEqual(query['COD_Litigante'], ['2'])

    def test_subject_not_found_is_not_success(self):
        self.driver._page.set_content(popup_html("NIÑO"))
        self.assertEqual(main.open_cons_lit_popup_from_litigantes(self.driver), (None, "row_not_found"))

    def test_eliminated_demandado_is_skipped_for_active_demandado(self):
        source = popup_html().replace('DTE.</td>', 'DDO.</td>').replace(
            "'100','1','0','0','0','N'", "'100','2','0','0','1','N'",
        )
        self.driver._page.set_content(source)
        url, reason = main.open_cons_lit_popup_from_litigantes(self.driver)
        self.assertEqual(reason, '')
        self.assertEqual(parse_qs(urlparse(url).query)['CRR_IdParte'], ['200'])

    def test_disabled_button_is_not_success(self):
        self.driver._page.set_content(popup_html(disabled=True))
        self.assertEqual(main.open_cons_lit_popup_from_litigantes(self.driver), (None, "button_disabled"))

    def test_empty_litigante_parameters_are_rejected(self):
        self.driver._page.set_content(popup_html(invalid_url=True))
        self.assertEqual(main.open_cons_lit_popup_from_litigantes(self.driver), (None, "litigante_parameters_missing"))

    def test_other_person_parameters_are_rejected(self):
        source = popup_html().replace("' + rut + '", "11111111")
        self.driver._page.set_content(source)
        self.assertEqual(main.open_cons_lit_popup_from_litigantes(self.driver), (None, "litigante_parameters_mismatch"))


class ExtractionRegressionTests(unittest.TestCase):
    def test_latin1_and_utf8_response_decoding(self):
        source = "<table><tr><th>Fec. Últ. trámite</th><th>Materia(Término)</th></tr></table>"
        for encoding in ("utf-8", "latin-1"):
            with self.subTest(encoding=encoding):
                with patch.object(main, 'download_session_resource', return_value=('text/html', source.encode(encoding))):
                    _, decoded = main.fetch_session_resource_text(None, '/test')
                self.assertEqual(decoded, source)

    def test_realistic_headers_and_placeholder_rows(self):
        source = """<table><tr><th>RIT</th><th>Fec. Ing.</th><th>Fec. Últ. trámite</th>
        <th>Tribunal</th><th>Tip. Lit.</th><th>Est. Procesal</th><th>Materia(Término)</th></tr>
        <tr><td>--</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
        <tr><td>Z-123-2026</td><td>01/01/2026</td><td>02/01/2026</td><td>Juzgado de prueba</td>
        <td>DDO.</td><td>Vigente</td><td>Materia de prueba</td></tr></table>"""
        rows = main.extract_cons_lit_rows_from_html(source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['RIT'], 'Z-123-2026')
        self.assertEqual(rows[0]['Fec. Últ. trámite'], '02/01/2026')


class DesktopRegressionTests(unittest.TestCase):
    def setUp(self):
        import app_tk
        self.worker_patch = patch.object(app_tk, 'BackendWorker')
        self.worker = self.worker_patch.start().return_value
        self.app = app_tk.SitfaApp()
        self.app.withdraw()

    def tearDown(self):
        self.app.destroy()
        self.worker_patch.stop()

    def test_cons_lit_rows_and_double_click_dispatch_history(self):
        rows = [{
            'RIT': f'Z-{index}-2026', 'Fec. Ing.': '01/01/2026',
            'Fec. Últ. trámite': '02/01/2026', 'Tribunal': 'Tribunal de prueba',
            'Materia(Término)': 'Materia de prueba',
        } for index in range(1, 51)]
        self.app._handle_result('consult_success', {'rit': 'Z-2248-2026', 'results': {'cons_lit': rows}})
        tree = self.app.cons_lit_tree
        self.assertEqual(len(tree.get_children()), 50)
        self.assertIn('50 causas', self.app.cons_lit_status_var.get())
        self.assertEqual(tree.item('2', 'values'), (
            'Z-2-2026', '01/01/2026', '02/01/2026', 'Tribunal de prueba', 'Materia de prueba',
        ))
        self.assertTrue(tree.bind('<Double-1>'))
        tree.selection_set('2')
        self.app._on_tree_double_click(SimpleNamespace(widget=tree))
        self.worker.submit.assert_called_once_with('history_detail', {'rit': 'Z-2-2026'})

    def test_history_rit_pdf_dispatch(self):
        self.app._fill_section('detail_history', [{'fecha': '01/01/2026', 'pdf_url': '/test.pdf'}])
        tree = self.app.detail_tree
        tree.selection_set('1')
        self.app._on_tree_double_click(SimpleNamespace(widget=tree))
        self.worker.submit.assert_called_once_with('open_pdf', {'pdf_url': '/test.pdf', 'prefix': 'historia_rit'})

    def test_missing_demandado_has_visible_explanation(self):
        self.app._handle_result('consult_success', {
            'rit': 'Z-123-2026', 'results': {'litigantes': [{'Sujeto': 'DTE.'}], 'cons_lit': []},
        })
        self.assertIn('no tiene litigante DDO.', self.app.cons_lit_status_var.get())
        self.assertFalse(self.app.cons_lit_tree.get_children())

    def test_empty_server_result_does_not_claim_rows_were_loaded(self):
        self.app._handle_result('consult_success', {
            'rit': 'Z-123-2026', 'results': {'litigantes': [{'Sujeto': 'DDO.'}], 'cons_lit': []},
        })
        self.assertIn('no devolvió causas', self.app.cons_lit_status_var.get())


if __name__ == '__main__':
    unittest.main()
