from utils.ids import new_uuid, build_source

def test_new_uuid_is_unique():
    assert new_uuid() != new_uuid()

def test_build_source():
    owner = "owner-uuid-123"
    file_uuid = "file-uuid-456"
    filename = "测试 简历.pdf"
    source = build_source(owner, file_uuid, filename)
    assert source == "owner-uuid-123__file-uuid-456__测试 简历.pdf"