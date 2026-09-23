"""domains：领域包命名空间（骨肉分离架构的「肉」的家）。

每个子包是一个可插拔领域：暴露 `get_pack() -> DomainPack`。
骨架只经 `domain.registry` 发现它们；领域包之间不得互相 import。
"""
