# Comprobación por mutaciones

Se rompe **una** salvaguarda a propósito en una copia temporal del proyecto y se comprueba que las pruebas lo notan (`python scripts/mutations.py`). `killed` = una prueba falló (bien); `SURVIVED` = las pruebas siguen pasando con la salvaguarda rota (un hueco).

| Id | Salvaguarda que se rompe | Archivo | Resultado | Prueba que la detecta |
|---|---|---|---|---|
| M01 | «máxima» ya no exige ser el mínimo histórico | `radar/priority.py` | **killed** | tests/test_priority.py::test_top_tier_must_also_be_the_lowest_price_ever_seen |
| M02 | una sola comparación entre tiendas puede dar «máxima» | `radar/priority.py` | **killed** | tests/test_priority.py::test_other_stores_alone_can_reach_alta_but_never_maxima |
| M03 | otra tienda más barata ya no baja el nivel | `radar/priority.py` | **killed** | tests/test_priority.py::test_a_cheaper_store_downgrades_the_alert_one_level |
| M04 | se asigna prioridad sin historial suficiente | `radar/priority.py` | **killed** | tests/test_priority.py::test_without_enough_history_nothing_is_extraordinary |
| M05 | el umbral «baja» pasa de ≥ a > | `radar/priority.py` | **killed** | tests/test_priority.py::test_tier_thresholds[8-baja] - AssertionError:... |
| M06 | una prioridad mayor ya no rompe el enfriamiento de 30 min | `radar/rules.py` | **killed** | tests/test_priority.py::test_cooldown_blocks_repeats_but_a_higher_priority_escalates |
| M07 | se quita el enfriamiento de 30 min | `radar/rules.py` | **killed** | tests/test_priority.py::test_cooldown_blocks_repeats_but_a_higher_priority_escalates |
| M08 | Telegram entrega por antigüedad y no por prioridad | `radar/notifications.py` | **killed** | tests/test_cli_delivery.py::test_the_most_important_alert_goes_first_then_oldest_first |
| M09 | un error HTTP puede acelerar el ritmo (AutoThrottle) | `radar/politeness.py` | **killed** | tests/test_restrictions.py::test_autothrottle_follows_latency_never_drops_on_errors_and_floors_the_gap |
| M10 | un rechazo (CAPTCHA, 403, 429...) ya no pausa el sitio | `radar/scheduler.py` | **killed** | tests/test_restrictions.py::test_a_captcha_pauses_the_domain_logs_the_incident_and_stops_the_requests |
| M11 | una salida apagada cae a la salida directa | `radar/egress.py` | **killed** | tests/test_restrictions.py::test_a_disabled_exit_refuses_its_domains_and_never_falls_back_to_direct |
| M12 | se acepta una tienda fuera de la lista permitida | `radar/providers/storepage.py` | **killed** | tests/test_providers.py::test_only_allow_listed_domains_are_ever_contacted |
| M13 | se acepta un dominio que resuelve a una IP privada (SSRF) | `radar/providers/storepage.py` | **killed** | tests/test_providers.py::test_domains_resolving_to_private_addresses_are_refused_without_a_request[127.0.0.1] |
| M14 | se interpreta cualquier tipo de contenido | `radar/providers/storepage.py` | **killed** | tests/test_restrictions.py::test_only_html_is_parsed_for_a_product_page |
| M15 | un 401/403 en robots.txt se toma como «sin reglas» | `radar/providers/storepage.py` | **killed** | tests/test_robots.py::test_missing_robots_means_no_rules_but_a_refusal_is_never_read_as_permission |
| M16 | robots.txt: gana la regla más corta en vez de la más larga | `radar/robots.py` | **killed** | tests/test_robots.py::test_the_most_specific_rule_wins_regardless_of_the_order_in_the_file |
| M17 | robots.txt: en empate gana Disallow en vez de Allow | `radar/robots.py` | **killed** | tests/test_robots.py::test_the_most_specific_rule_wins_regardless_of_the_order_in_the_file |
| M18 | los registros dejan de borrar credenciales | `radar/security.py` | **killed** | tests/test_restrictions.py::test_secrets_are_masked_and_log_text_is_neutralised |
| M19 | dos instancias pueden vigilar a la vez (lease) | `radar/scheduler.py` | **killed** | tests/test_scheduler.py::test_a_second_instance_never_runs_jobs_while_the_first_holds_the_lease |

**19 de 19 mutaciones detectadas por las pruebas.**


## Nota de honestidad

En la primera pasada **M19 sobrevivió**: ninguna prueba comprobaba que `tick()` respeta el *lease* (había pruebas del lease, pero ninguna que verificara que una segunda instancia no ejecuta trabajos). Era un hueco real: se añadió `test_a_second_instance_never_runs_jobs_while_the_first_holds_the_lease` y la mutación se repitió (ahora detectada). Los resultados de arriba son los de la segunda pasada.

Límites: son 19 mutaciones elegidas a mano sobre las salvaguardas que más importan, no una cobertura exhaustiva. Se ejecutaron por tandas (`--only`, `--results`, `--report`).
