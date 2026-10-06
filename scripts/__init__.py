"""scripts 包：数据处理与验证脚本。

包化的目的：让 tests/ 与 backend/ 能直接导入
`scripts.commerce_schema` / `scripts.generate_demo_data` / `scripts.validate_demo_data`
中的纯函数（pythonpath=. 已在 pytest.ini 配置）。
"""
