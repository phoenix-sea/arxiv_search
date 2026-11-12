from arxiv_search.orgparse import parse_block

# The user's example author block, verbatim, and the values they expect from it:
# "World Bank Group or worldbankgroup.org or Development Impact AI Lab, World Bank Group".
BLOCK = """Giuliano Martinelli*, Abelardo Carlos Martinez Lorenzo, Satvik Garg, Linxi Wang
Riccardo Orlando
Piriyakorn Piriyatamwong
Samuel Paul FraibergerDevelopment Impact AI Lab, World Bank Group
*Lead author. Correspondence: sfraiberger@worldbankgroup.org"""

AUTHORS = ["Giuliano Martinelli", "Abelardo Carlos Martinez Lorenzo", "Satvik Garg", "Linxi Wang",
           "Riccardo Orlando", "Piriyakorn Piriyatamwong", "Samuel Paul Fraiberger"]


def test_user_example():
    [org] = parse_block(BLOCK, AUTHORS)
    assert org.name == "World Bank Group"
    assert org.domain == "worldbankgroup.org"
    assert org.unit == "Development Impact AI Lab"
