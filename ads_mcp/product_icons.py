"""Product labels and original Google Material Icons (Apache-2.0)."""

from base64 import b64decode

# https://github.com/google/material-design-icons/tree/master/png/action
# These are functional illustrations, with the original PNGs served locally.
PRODUCTS = {
    # trending_up
    "google": (
        "Google Ads",
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAQAAABIkb+zAAABYklEQVR42u2WvXmDMBRF7whpswf+o/ZK"
            "qrNFhmEED2NsV3FFChfGQRICSw9uvnuo+XQOQg8AIYQQQgghhBBCCCGW4AvdpIs+gX4X6F8k+rNAf5zp"
            "JxL9UKX/LqyeGj/MATVuzDswrl804NNAvyv79Fxx/a705rus+nergP7yLpv+DbVnqBocPZdN3/ddyM7B"
            "s7zLpj9MyK5/9R4195b+taf/N8FEPzUhpH+I/GBkZB/RT0lI1+8nmOmPJUzTfyYY6scSQvr70T9VU/1Q"
            "wjz9R0Ix/TOOOCUlzNdHSf0KwEdCwsL6O1yC+khI8OtfsFuH/lgCgX4s4ZtDP5ZAop+eYKa/naifltBi"
            "a6XfTtYfT1i9fjyBQj+c0GJjo795U9+fQKU/TKDTf02g1H8mzL9/Yf1HQmOlXxXQN6TCmVkfaLj1fXOb"
            "Sn+YQKf/mkCpv8DcLpPQMOsLIYQQQgghhBBCiP/NL6ae/YpUnP2AAAAAAElFTkSuQmCC"
        ),
    ),
    # work_outline
    "linkedin": (
        "LinkedIn Ads",
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAQAAABIkb+zAAABMklEQVR42u3bOwoCMRSF4dOIS7HRddqm"
            "kdmM4AKsswfrEd/GWAliYyaj5Ib8J63F+cBh5sKNRAghhPwiCzl57RW/nL28nBa2yk/VKXyt/n6COk3t"
            "1N8MKv86GyuELqt+VFRn478fsgHBwrPgsutHRbnyAD8K4MsD+lGAvjzgs9Kvfw8AAAAAAMoA5onf9/8+"
            "WfPDRCvdi1fPnh8mWpsqP3h+cCbrJ88PM93MApLmh6XZ+onzw9Y0IGF+2JkGJMwPB2NvysEvPgClAUcA"
            "AADUDTgBANA44AwAQOOACwAAAOoGXAEAaBxwAwCgccAdAIDGAQEAAAB1Ax4AADQOiAAKA/ralz187es2"
            "TpUvPI25UmJi5WzMpR4TS3/516rMrF3mXGwztfj6ehYqXj0mhBBCCGkjTwce4p0n5eCiAAAAAElFTkSu"
            "QmCC"
        ),
    ),
    # label_outline
    "gtm": (
        "Google Tag Manager",
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAYAAADimHc4AAACKklEQVR4Ae3bAWRbURiG4eGguAiCIgiG"
            "AoKiGMpQDMUwFDAUQzAUxTAUQxEExVAMRTEEwVAMQzAUQRAEQVEUwd8PALQ/zi/n3vu+PAA4PpojOX1D"
            "L0RERERERERERERERERERD05l6nMZVFDUzmTXalNSb7Lk1hDPMlXSVJ0SSZiDfVTim4k1nDfpMgGYi2w"
            "kXdSXFdiLbGQjhTVQqxFrqWorIU+1WeA+vXHcaYH6TNATD15cJzrThIDxPRRzOGMAbZ7u9vIPgPEVDlv"
            "eHOpGCCmA9k4zjhmgLjOxRyOGSCmJHeOc66lxwAx9eXRcdYpA8R1IuYwZIC4fjl/xBkwQEwd59X0v+ww"
            "QEyHzqvpiAHiuhBzOGKAmJL8c5x9JV0GiOmt82r6mwHi+izm8IUB4roVe8Wj7DFATF1Zir1iJokBYnov"
            "5vCDAWIayGY7LyoYoJJ755uiigHyN3b+dHkgmWOAD9t7V8oAu7J2nP+vJAbI38R5/+9L5hhgKOZwIplj"
            "gIHzv4BuJHMMsCMzx5mX0mGA/F06r5yHkjkGOBJzuJDMMUBXVgFfuDFA+V85M8CpmMOpZI4B9pw/O95K"
            "5hggySz+h3cGiH96wgBhj68uJXMM0JHl9p4fMsBN6APcgKyFhlJMK7EWmUhRXYu1xFp6Jd4YrCWOy//g"
            "aq6xFFsls4b/3U9SdFVDPw9GUklt2pcrmctGrIbmMs551yciIiIiIiIiIiIiIiIiInoGwD9J/e55gyEA"
            "AAAASUVORK5CYII="
        ),
    ),
}


def product_icon(key):
    return f'<span class="product-icon"><img src="/icons/{key}.png" width="26" height="26" alt=""></span>'
