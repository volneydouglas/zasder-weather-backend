"""Installer for the Zasder Weather WeeWX uploader.

    weectl extension install <path-or-url-to-this-package>   # WeeWX 5
    wee_extension --install <path>                           # WeeWX 4
"""

from weecfg.extension import ExtensionInstaller


def loader():
    return ZasderInstaller()


class ZasderInstaller(ExtensionInstaller):
    def __init__(self):
        super().__init__(
            version="1.0.0",
            name="zasder",
            description="Send WeeWX archive records to a Zasder Weather server",
            author="Zasder Weather",
            author_email="support@zasder.com",
            restful_services="user.zasder.Zasder",
            config={
                "StdRESTful": {
                    "Zasder": {
                        "server_url": "replace_me",
                        "ingest_token": "replace_me",
                    },
                },
            },
            files=[("bin/user", ["bin/user/zasder.py"])],
        )
