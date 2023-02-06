def isStringEmpty(input):
    return input is not None and isinstance(input, str) and len(input.strip()) > 0


def ordinalize(input):
    input = int(input)
    assert input in [1, 2, 3, 4]
    if input == 1:
        return '1st'
    if input == 2:
        return '2nd'
    if input == 3:
        return '3rd'
    if input == 4:
        return '4th'
