"""Static catalogue: watch kinds and product categories. No prices, no network."""

KINDS = {
    "flight": {"label": "Vuelo", "group": "viajes"},
    "bus": {"label": "Bus", "group": "viajes"},
    "product": {"label": "Producto", "group": "tienda"},
}

# Product categories requested for the monitor. Keys are stable identifiers (ASCII).
CATEGORIES = {
    "alimentos": {
        "label": "Alimentos y bebidas",
        "subcategories": ["abarrotes", "bebidas", "frescos", "snacks", "otros"],
    },
    "ropa": {
        "label": "Ropa y calzado",
        "subcategories": [
            "hombre",
            "mujer",
            "ninos",
            "calzado",
            "deportiva",
            "accesorios",
            "otros",
        ],
    },
    "juguetes": {
        "label": "Juguetes",
        "subcategories": [
            "construccion",
            "munecas",
            "vehiculos",
            "juegos_de_mesa",
            "educativos",
            "otros",
        ],
    },
    "electrodomesticos": {
        "label": "Electrodomésticos",
        "subcategories": [
            "refrigeracion",
            "lavado",
            "cocina",
            "climatizacion",
            "pequenos",
            "otros",
        ],
    },
    "tecnologia": {
        "label": "Tecnología",
        "subcategories": [
            "laptops",
            "televisores",
            "consolas",
            "videojuegos",
            "celulares",
            "monitores",
            "audio",
            "perifericos",
            "almacenamiento",
            "redes",
            "componentes_gpu",
            "componentes_cpu",
            "componentes_ram",
            "componentes_placas",
            "componentes_fuentes",
            "componentes_refrigeracion",
            "otros",
        ],
    },
}

TRAVEL_CATEGORY = "viajes"
TRAVEL_SUBCATEGORY = {"flight": "aereo", "bus": "terrestre"}


def resolve_category(kind, category, subcategory):
    """Return a validated (category, subcategory) pair for a watch kind."""
    if kind in TRAVEL_SUBCATEGORY:
        if category not in (None, TRAVEL_CATEGORY):
            raise ValueError("Los pasajes usan la categoría «viajes».")
        return TRAVEL_CATEGORY, TRAVEL_SUBCATEGORY[kind]
    if category not in CATEGORIES:
        raise ValueError(
            "Categoría de producto desconocida. Usa: " + ", ".join(CATEGORIES) + "."
        )
    if subcategory is not None and subcategory not in CATEGORIES[category]["subcategories"]:
        raise ValueError("Subcategoría no válida para la categoría elegida.")
    return category, subcategory


def public_catalog():
    return {
        "kinds": KINDS,
        "categories": CATEGORIES,
    }
