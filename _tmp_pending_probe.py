import os
from main import LOGIN_URL, create_driver, login_to_sitfa, wait_for_ready

driver = create_driver(initial_url=LOGIN_URL, browser="chrome", headless=True)
try:
    login_to_sitfa(driver, os.environ["SITFA_USER"], os.environ["SITFA_PASS"])
    for tipo in (1, 2):
        url = f"http://www.familia.pjud/SITFAWEB/TrmPendientesViewAccion.do?TipoTramite={tipo}"
        driver.get(url)
        wait_for_ready(driver, timeout=15)
        info = driver.execute_script("""
        return {
          title: document.title,
          url: location.href,
          text: (document.body && (document.body.innerText || document.body.textContent) || '').slice(0, 5000),
          forms: Array.from(document.forms || []).map(function(form) {
            return {name: form.name || '', action: form.action || '', controls: Array.from(form.elements || []).map(function(el) {
              return {tag: el.tagName, name: el.name || '', type: el.type || '', value: el.value || '', id: el.id || ''};
            })};
          })
        };
        """)
        print("TIPO", tipo, info)
finally:
    driver.quit()
