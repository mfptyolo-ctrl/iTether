"""Edge-case tests for the descriptor decision tree.

These cover real-world corner cases that the basic test set doesn't
exercise: iOS 18 omitting the CDC Union descriptor, two NCM controls
that are *both* interrupt-bearing, and the class-code tiebreaker rule.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from itether_core.descriptor import (
    AppleDeviceInfo,
    InterfaceDescriptor,
    TetheringProtocol,
    identify_tethering_function,
    parse_lsusb_dump,
)


def make_iface(*, num: int, klass: int, sub: int, proto: int = 0x00,
               intr: bool = False, intr_ep: int | None = None,
               slave: int | None = None,
               bulk_in: int | None = None, bulk_out: int | None = None,
               alt: int = 1) -> InterfaceDescriptor:
    return InterfaceDescriptor(
        number=num,
        alternate=alt,
        class_code=klass,
        subclass=sub,
        protocol=proto,
        has_interrupt_in=intr,
        interrupt_in_ep=intr_ep,
        cdc_union_slave=slave,
        bulk_in_ep=bulk_in,
        bulk_out_ep=bulk_out,
        is_ncm_function=(klass == 0x02 and sub in (0x0D, 0x0E)) or (klass == 0xFF and sub == 0xFD and intr),
    )


class NoUnionDescriptorTests(unittest.TestCase):
    """iOS 18 sometimes omits the CDC Union descriptor entirely.

    The parser must fall back to the +1 heuristic so the data interface
    is still correctly identified.
    """

    def test_no_union_data_interface_falls_back_to_plus_one(self) -> None:
        info = AppleDeviceInfo(
            vendor_id=0x05AC,
            product_id=0x12A8,
            interfaces=(
                make_iface(num=2, klass=0x02, sub=0x0D, intr=True,
                           intr_ep=0x86, slave=None),
                make_iface(num=3, klass=0x0A, sub=0x00, proto=0x01,
                           bulk_in=0x87, bulk_out=0x05),
            ),
        )
        cand = identify_tethering_function(info)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.protocol, TetheringProtocol.CDC_NCM)
        self.assertEqual(cand.control_interface, 2)
        self.assertEqual(cand.data_interface, 3,
                         "missing Union descriptor should fall back to +1")


class InterruptPriorityTests(unittest.TestCase):
    """Two NCM control functions: the interrupt-bearing one wins."""

    def test_interrupt_bearing_wins_over_no_interrupt(self) -> None:
        info = AppleDeviceInfo(
            vendor_id=0x05AC,
            product_id=0x12A8,
            interfaces=(
                # Lower-numbered NCM control without interrupt endpoint
                # (this is the RemoteXPC twin).
                make_iface(num=2, klass=0x02, sub=0x0D, intr=False,
                           slave=3),
                make_iface(num=3, klass=0x0A, sub=0x00, proto=0x01,
                           bulk_in=0x87, bulk_out=0x05),
                # Higher-numbered NCM control WITH interrupt endpoint
                # (this is the actual tethering function).
                make_iface(num=4, klass=0x02, sub=0x0D, intr=True,
                           intr_ep=0x88, slave=5),
                make_iface(num=5, klass=0x0A, sub=0x00, proto=0x01,
                           bulk_in=0x89, bulk_out=0x06),
            ),
        )
        cand = identify_tethering_function(info)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.control_interface, 4,
                         "interrupt-bearing function at higher number must win")
        self.assertEqual(cand.data_interface, 5)
        self.assertTrue(cand.rejected_remotexpc)
        self.assertTrue(cand.has_interrupt_endpoint)

    def test_both_interrupt_bearing_lower_number_wins(self) -> None:
        info = AppleDeviceInfo(
            vendor_id=0x05AC,
            product_id=0x12A8,
            interfaces=(
                make_iface(num=2, klass=0x02, sub=0x0D, intr=True,
                           intr_ep=0x86, slave=3),
                make_iface(num=3, klass=0x0A, sub=0x00, proto=0x01,
                           bulk_in=0x87, bulk_out=0x05),
                make_iface(num=4, klass=0x02, sub=0x0D, intr=True,
                           intr_ep=0x88, slave=5),
                make_iface(num=5, klass=0x0A, sub=0x00, proto=0x01,
                           bulk_in=0x89, bulk_out=0x06),
            ),
        )
        cand = identify_tethering_function(info)
        self.assertIsNotNone(cand)
        # Both interrupt-bearing: lower number wins (matches Apple's choice).
        self.assertEqual(cand.control_interface, 2)


class ClassCodeTiebreakerTests(unittest.TestCase):
    """Standard CDC NCM (class 0x02/0x0D) wins over Apple-private (0xFF/0xFD)."""

    def test_standard_ncm_wins_over_apple_private(self) -> None:
        info = AppleDeviceInfo(
            vendor_id=0x05AC,
            product_id=0x12A8,
            interfaces=(
                # Apple-private NCM at lower number (no interrupt endpoint).
                make_iface(num=2, klass=0xFF, sub=0xFD, intr=False,
                           slave=None),
                # Standard CDC NCM at higher number (with interrupt).
                make_iface(num=3, klass=0x02, sub=0x0D, intr=True,
                           intr_ep=0x88, slave=4),
                make_iface(num=4, klass=0x0A, sub=0x00, proto=0x01,
                           bulk_in=0x89, bulk_out=0x06),
            ),
        )
        cand = identify_tethering_function(info)
        self.assertIsNotNone(cand)
        # Standard NCM should be picked even at higher number, because
        # it has the interrupt endpoint.
        self.assertEqual(cand.protocol, TetheringProtocol.CDC_NCM)
        self.assertEqual(cand.control_interface, 3)


class ParserRegressionTests(unittest.TestCase):
    """Sanity-checks on the textual lsusb parser."""

    def test_real_ios_lsusb_dump_iphone6s(self) -> None:
        """An actual ``lsusb -vvv`` capture from an iPhone 6s running iOS 15.

        This is the *only* ipheth-only layout (no NCM twin). The parser
        must select the ipheth bulk pair at interface 2.
        """
        text = """
Bus 002 Device 007: ID 05ac:12a8 Apple, Inc. iPhone 5/5C/5S/6/SE/7/8/X/XR
Device Descriptor:
  idVendor           0x05ac
  idProduct          0x12a8
  iManufacturer           1 Apple Inc.
  iProduct                2 iPhone
  iSerial                 3 abcdef0123456789
Configuration Descriptor:
  bNumInterfaces          3
  Interface Descriptor:
    bLength                 9
    bInterfaceNumber        2
    bAlternateSetting       1
    bNumEndpoints           2
    bInterfaceClass       255 Vendor Specific Class
    bInterfaceSubClass    253
    bInterfaceProtocol      1
    Endpoint Descriptor:
      bLength             7
      bEndpointAddress 0x86  EP 6 IN
      bmAttributes            2
        Transfer Type            Bulk
        Synch Type               None
        Usage Type               Data
      wMaxPacketSize     0x0200  1x 512 bytes
    Endpoint Descriptor:
      bLength             7
      bEndpointAddress 0x05  EP 5 OUT
      bmAttributes            2
        Transfer Type            Bulk
        Synch Type               None
        Usage Type               Data
      wMaxPacketSize     0x0200  1x 512 bytes
"""
        devs = parse_lsusb_dump(text)
        self.assertEqual(len(devs), 1)
        cand = identify_tethering_function(devs[0])
        self.assertIsNotNone(cand)
        self.assertEqual(cand.protocol, TetheringProtocol.IPHETH)


if __name__ == "__main__":
    unittest.main()
