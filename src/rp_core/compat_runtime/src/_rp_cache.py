# SPDX-License-Identifier: AGPL-3.0-only
"""Load legacy NumPy embedding caches without arbitrary pickle globals."""
import importlib
import pickle


class EmbeddingUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        modules = {"numpy", "numpy.core.multiarray", "numpy._core.multiarray", "numpy.core.numeric", "numpy._core.numeric"}
        allowed = {"numpy": {"ndarray", "dtype"}, "multiarray": {"_reconstruct", "scalar"}, "numeric": {"_frombuffer"}}
        key = module if module == "numpy" else module.rsplit(".", 1)[-1]
        if module not in modules or name not in allowed.get(key, set()):
            raise pickle.UnpicklingError("Non-data pickle global refused.")
        return getattr(importlib.import_module(module), name)


def load(stream):
    return EmbeddingUnpickler(stream).load()
