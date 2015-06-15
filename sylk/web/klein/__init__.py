
# Vendored Klein from: https://github.com/twisted/klein
# Changes from original version:
#  - Removed global Klein app
#  - Removed Klein.run method
#  - Fixed imports to work inside SylkServer


from sylk.web.klein.app import Klein

__all__ = ['Klein']

