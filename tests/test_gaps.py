import pytest

def test_identity_is_open_gap():
    from src.listingtrail import identity
    with pytest.raises(NotImplementedError):
        identity.process({"id": "x"})

def test_statusline_is_open_gap():
    from src.listingtrail import statusline
    with pytest.raises(NotImplementedError):
        statusline.process({"id": "x"})
