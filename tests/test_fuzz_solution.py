import random
import logging
from collections import defaultdict
import string
from typing import DefaultDict
from woke.testing import *
from woke.testing.fuzzing import *

from pytypes.contracts.MyContract import MyContract
from pytypes.contracts.testing.AxelarGatewayMock import AxelarGatewayMock
from pytypes.axelarnetwork.axelargmpsdksolidity.contracts.interfaces.IERC20 import IERC20
from pytypes.axelarnetwork.axelargmpsdksolidity.contracts.test.ERC20MintableBurnable import ERC20MintableBurnable
from pytypes.axelarnetwork.axelargmpsdksolidity.contracts.deploy.Create3Deployer import Create3Deployer
from pytypes.axelarnetwork.axelargmpsdksolidity.contracts.interfaces.IAxelarExecutable import IAxelarExecutable

chain1 = Chain()
chain2 = Chain()

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def on_revert(f):
    def wrapper(*args, **kwargs):
        try:
            f(*args, **kwargs)
        except TransactionRevertedError as e:
            if e.tx is not None:
                print(e.tx.call_trace)
                raise
        else:
            raise

    return wrapper


class T(FuzzTest):
    balances: DefaultDict[IERC20, DefaultDict[Address, int]]
    deployer: Address
    tokens: List[ERC20MintableBurnable]
    my_contract1: MyContract
    my_contract2: MyContract
    gw1: AxelarGatewayMock
    gw2: AxelarGatewayMock

    def pre_sequence(self) -> None:
        self.balances = defaultdict(lambda: defaultdict(int))
        self.deployer = random_account(chain=chain1).address
        self.tokens = []

        self.gw1 = AxelarGatewayMock.deploy(from_=self.deployer, chain=chain1)
        self.gw2 = AxelarGatewayMock.deploy(from_=self.deployer, chain=chain2)

        deployer1 = Create3Deployer.deploy(from_=self.deployer, chain=chain1)
        deployer2 = Create3Deployer.deploy(from_=self.deployer, chain=chain2)
        salt = random_bytes(32)
        self.my_contract1 = MyContract(
            deployer1.deploy_(
                MyContract.get_creation_code() + Abi.encode(["address"], [self.gw1]),
                salt,
                from_=self.deployer).return_value,
            chain=chain1,
        )
        self.my_contract2 = MyContract(
            deployer2.deploy_(
                MyContract.get_creation_code() + Abi.encode(["address"], [self.gw2]),
                salt,
                from_=self.deployer).return_value,
            chain=chain2,
        )
        assert self.my_contract1.address == self.my_contract2.address

    def relay(self, tx: TransactionAbc) -> None:
        for event in tx.events:
            if isinstance(event, AxelarGatewayMock.ContractCallWithToken):
                if event.destinationChain == "chain1":
                    dest_chain = chain1
                    dest_gw = self.gw1
                    src_chain_name = "chain2"
                    src_contract = self.my_contract2
                elif event.destinationChain == "chain2":
                    dest_chain = chain2
                    dest_gw = self.gw2
                    src_chain_name = "chain1"
                    src_contract = self.my_contract1
                else:
                    raise ValueError(f"Unknown destination chain: {event.destinationChain}")

                tx = IAxelarExecutable(event.destinationContractAddress, dest_chain).executeWithToken(
                    random_bytes(32),
                    src_chain_name,
                    str(src_contract.address),
                    event.payload,
                    event.symbol,
                    event.amount,
                    from_=dest_gw,
                )

    @flow(max_times=20)
    def flow_deploy(self):
        name = random_string(5, 10)
        symbol = random_string(3, 3, alphabet=string.ascii_uppercase)
        token1 = ERC20MintableBurnable.deploy(name, symbol, 18, from_=self.deployer, chain=chain1)
        token2 = ERC20MintableBurnable.deploy(name, symbol, 18, from_=self.deployer, chain=chain2)
        self.tokens.append(token1)
        self.tokens.append(token2)
        self.gw1.registerToken(token1, from_=self.deployer)
        self.gw2.registerToken(token2, from_=self.deployer)

        logger.debug(f"Deployed {token1.symbol()} at {token1.address} and {token2.symbol()} at {token2.address}")

    @flow()
    def flow_mint(self):
        if len(self.tokens) == 0:
            return

        token = random.choice(self.tokens)
        recipient = random_account(chain=token.chain)
        amount = random_int(0, 100_000)
        token.mint(recipient, amount, from_=random_address())

        self.balances[IERC20(token)][recipient.address] += amount

        logger.debug(f"Minted {amount} {token.symbol()} to {recipient.address}")

    @flow()
    def flow_bridge(self):
        t = [t for t in self.tokens if sum(self.balances[IERC20(t)].values()) > 0]
        if len(t) == 0:
            return

        token = random.choice(t)
        sender = random.choice([a for a in token.chain.accounts if self.balances[IERC20(token)][a.address] > 0])
        balance = self.balances[IERC20(token)][sender.address]

        transfer_data: List[MyContract.TransferData] = []
        token_sum = 0
        for _ in range(random_int(0, 10)):
            amount = random_int(0, balance, zero_prob=0.1)
            balance -= amount
            token_sum += amount

            transfer_data.append(MyContract.TransferData(
                amount=amount,
                recipient=random_account(chain=chain1).address,
                payload=bytearray(),
            ))
        random.shuffle(transfer_data)

        if token.chain == chain1:
            dest_token = IERC20(self.gw2.tokenAddresses(token.symbol()), chain=chain2)
            token.approve(self.my_contract1, token_sum, from_=sender)
            tx = self.my_contract1.bridge("chain2", token.symbol(), transfer_data, from_=sender)
        else:
            dest_token = IERC20(self.gw1.tokenAddresses(token.symbol()), chain=chain1)
            token.approve(self.my_contract2, token_sum, from_=sender)
            tx = self.my_contract2.bridge("chain1", token.symbol(), transfer_data, from_=sender)
        
        self.relay(tx)

        logger.debug(f"Bridged {token_sum} {token.symbol()} from {sender.address} to {tx.chain}")

        self.balances[IERC20(token)][sender.address] -= token_sum
        for transfer in transfer_data:
            self.balances[dest_token][transfer.recipient] += transfer.amount

    @invariant(period=50)
    def invariant_balances(self):
        for token in self.tokens:
            for account in token.chain.accounts:
                assert token.balanceOf(account) == self.balances[IERC20(token)][account.address]
        
        logger.debug("Balances are consistent")


@chain1.connect(chain_id=10, accounts=20)
@chain2.connect(chain_id=20, accounts=20)
@on_revert
def test_fuzz():
    assert all([a.address == b.address for a, b in zip(chain1.accounts, chain2.accounts)])
    T().run(10, 1000)
