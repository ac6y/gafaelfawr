"""Mock Firestore API for testing."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional
from unittest.mock import MagicMock, Mock, patch

from google.cloud import firestore

__all__ = ["MockFirestore", "patch_firestore"]


class MockDocument:
    """Mock document contents."""

    def __init__(self, data: Optional[Dict[str, Any]]) -> None:
        self._data = data
        self.exists = data is not None

    def __getitem__(self, key: str) -> Optional[Any]:
        assert self._data
        return self._data.get(key)


class MockDocumentRef(Mock):
    """Mock document reference."""

    def __init__(self) -> None:
        super().__init__(spec=firestore.AsyncDocumentReference)
        self.document: Optional[Dict[str, Any]] = None

    async def get(self, *, transaction: MockTransaction) -> MockDocument:
        assert isinstance(transaction, MockTransaction)
        return MockDocument(self.document)


class MockCollection(Mock):
    """Mock Firestore collection object."""

    def __init__(self) -> None:
        super().__init__(spec=firestore.AsyncCollectionReference)
        self._documents: Dict[str, MockDocumentRef] = defaultdict(
            MockDocumentRef
        )

    def document(self, name: str) -> MockDocumentRef:
        return self._documents[name]


class MockTransaction(MagicMock):
    """Mock Firestore transaction."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(spec=firestore.AsyncTransaction, **kwargs)
        self._id = None
        self._max_attempts = 1

    def create(self, ref: MockDocumentRef, data: Dict[str, Any]) -> None:
        assert ref.document is None
        ref.document = data

    def delete(self, ref: MockDocumentRef) -> None:
        ref.document = None

    def update(self, ref: MockDocumentRef, data: Dict[str, Any]) -> None:
        assert ref.document is not None
        ref.document.update(data)


class MockFirestore(Mock):
    """Mock Firestore API for testing.

    This mock should be installed with ``mock_firestore``.

    Parameters
    ----------
    config : `gafaelfawr.config.FirestoreConfig`
        Configuration for Google Firestore.
    """

    def __init__(self) -> None:
        super().__init__()
        self._collections: Dict[str, MockCollection] = defaultdict(
            MockCollection
        )

    def collection(self, name: str) -> MockCollection:
        return self._collections[name]

    def transaction(self) -> MockTransaction:
        return MockTransaction()


@contextmanager
def patch_firestore() -> Iterator[MockFirestore]:
    """Mock the Firestore API for testing.

    Returns
    -------
    mock : `MockFirestore`
        The mock Firestore API.
    """
    mock = MockFirestore()
    with patch.object(firestore, "AsyncClient", return_value=mock):
        yield mock