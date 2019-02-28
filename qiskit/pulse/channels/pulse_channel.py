ç
from abc import ABCMeta


class PulseChannel(metaclass=ABCMeta):
    """Pulse Channel."""

    def __str__(self):
        return '%s%d'.format(self.__class__.prefix, self.index)

    def name(self):
        return str(self)

    def __eq__(self, other):
        """Two channels are the same if they are of the same type, and have the same index.

        Args:
            other (PulseChannel): other PulseChannel

        Returns:
            bool: are self and other equal.
        """
        if type(self) is type(other) and \
                self.index == other.index:
            return True
        return False

    def __hash__(self):
        """Make object hashable, based on the index to hash."""
        return hash((type(self), self.index))
