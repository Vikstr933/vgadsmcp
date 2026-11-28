"""Original trademarks used to identify integrations inside Viks."""

from importlib.resources import files

PRODUCTS = {
    "drive": ("Google Drive", "google-drive.png", "image/png"),
    "ga4": ("Google Analytics 4", "google-analytics.svg", "image/svg+xml"),
    # https://about.google/brand-resource-center/products-and-services/
    "google": ("Google Ads", "google-ads.webp", "image/webp"),
    # https://brand.linkedin.com/downloads — in-logo.zip, LI-In-Bug.png
    "linkedin": ("LinkedIn Ads", "linkedin-in.png", "image/png"),
    # https://www.gstatic.com/analytics-suite/header/suite/v2/ic_tag_manager.svg
    "gtm": ("Google Tag Manager", "google-tag-manager.svg", "image/svg+xml"),
}


def product_icon(key):
    _, filename, _ = PRODUCTS[key]
    return (
        f'<span class="product-icon"><img src="/icons/{filename}" '
        'width="32" height="32" alt=""></span>'
    )


def product_icon_assets():
    directory = files("ads_mcp").joinpath("brand_assets")
    return {
        f"/icons/{filename}": (
            directory.joinpath(filename).read_bytes(),
            media_type,
        )
        for _, filename, media_type in PRODUCTS.values()
    }
