"""Step 0: introspect qdrant-edge-py's real API before building anything on top of it."""
import inspect
import pkgutil
import importlib

import qdrant_edge as edge

print("=== module file ===")
print(edge.__file__)
print("version:", getattr(edge, "__version__", "?"))

print("\n=== top-level names ===")
for name in sorted(dir(edge)):
    if name.startswith("_"):
        continue
    obj = getattr(edge, name)
    print(f"{name}: {type(obj)}")

print("\n=== submodules ===")
if hasattr(edge, "__path__"):
    for m in pkgutil.iter_modules(edge.__path__):
        print(m.name)


def dump_class(cls):
    print(f"\n--- class {cls.__module__}.{cls.__name__} ---")
    try:
        sig = inspect.signature(cls.__init__)
        print("__init__", sig)
    except (ValueError, TypeError) as e:
        print("__init__ signature unavailable:", e)
    for name, member in inspect.getmembers(cls):
        if name.startswith("_"):
            continue
        if inspect.isfunction(member) or inspect.ismethod(member):
            try:
                print(f"  {name}{inspect.signature(member)}")
            except (ValueError, TypeError):
                print(f"  {name}(...)")


candidates = []
for name in dir(edge):
    if name.startswith("_"):
        continue
    obj = getattr(edge, name)
    if inspect.isclass(obj):
        candidates.append(obj)

print("\n=== classes found ===")
for c in candidates:
    print(c)

for c in candidates:
    dump_class(c)
