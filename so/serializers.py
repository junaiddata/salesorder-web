from rest_framework import serializers
from .models import SalesOrder, OrderItem, Customer, Salesman, Items, CustomerPrice

class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ('item', 'quantity', 'price', 'is_custom_price')

class SalesOrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, write_only=True)
    class Meta:
        model = SalesOrder
        fields = ('id', 'customer', 'salesman', 'items', 'total_amount')
    def create(self, validated_data):
        items_data = validated_data.pop('items')
        sales_order = SalesOrder.objects.create(**validated_data)
        for item_data in items_data:
            OrderItem.objects.create(order=sales_order, **item_data)
        sales_order.total_amount = sum([od['quantity'] * float(od['price']) for od in items_data])
        sales_order.save()
        return sales_order

class CustomerSerializer(serializers.ModelSerializer):
    salesman_name = serializers.CharField(source='salesman.salesman_name', read_only=True)
    
    class Meta:
        model = Customer
        fields = ['id', 'customer_code', 'customer_name', 'salesman', 'salesman_name']

class SalesmanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Salesman
        fields = ('id', 'salesman_name')

class ItemsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Items
        fields = ('id', 'item_description','item_firm','item_price')


class OrderItemDetailSerializer(serializers.ModelSerializer):
    line_total = serializers.SerializerMethodField()
    item_description = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ('id', 'item', 'item_description', 'quantity', 'price', 'is_custom_price', 'line_total')

    def get_line_total(self, obj):
        return float(obj.quantity) * float(obj.price)

    def get_item_description(self, obj):
        return obj.item.item_description if obj.item else ""

class SalesOrderListSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.customer_name', read_only=True)
    class Meta:
        model = SalesOrder
        fields = ('id', 'order_date', 'customer','customer_name', 'salesman', 'total_amount', 'order_taken')

class SalesOrderDetailSerializer(serializers.ModelSerializer):
    items = OrderItemDetailSerializer(many=True)   # Just this, no source= needed!
    class Meta:
        model = SalesOrder
        fields = ('id', 'order_date', 'customer', 'salesman', 'total_amount', 'order_taken', 'items')

